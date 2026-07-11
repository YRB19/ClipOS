"""Automated pipeline: calls each existing core function in sequence.

This is the RQ worker entrypoint — it must be importable at module level
so rq can pickle and execute it.  Each stage is wrapped in try/except so
a failure at any point sets status="failed" and stops the chain.
"""

import logging
from pathlib import Path

from app.config import settings
from app.core.captions import generate_captions
from app.core.clip_cutter import cut_clips
from app.core.highlights import detect_highlights
from app.core.reframe import reframe_clip
from app.core.scoring import score_and_title_clips
from app.core.transcribe import transcribe_video
from app.models.job import Job, engine
from sqlmodel import Session

logger = logging.getLogger(__name__)


def _fail(job_id: str, stage: str, exc: Exception) -> None:
    """Set the job to failed with an error message and log it."""
    msg = f"{stage}: {exc}"
    logger.error("Pipeline failed for job %s — %s", job_id, msg)
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job:
            job.status = "failed"
            job.error_message = msg
            session.add(job)
            session.commit()


def _load_clip_ids(job_id: str) -> list[str]:
    """Read clip_ids from the highlights.json produced by cut_clips."""
    highlights_path = Path(settings.storage_raw) / job_id / "highlights.json"
    import json
    data = json.loads(highlights_path.read_text())
    return [h["clip_id"] for h in data if "clip_id" in h]


def run_pipeline(job_id: str) -> None:
    """Execute the full ClipOS pipeline for a single job.

    Status progression (same as the manual path):
        ingested → transcribed → highlights_ready → clips_ready → done

    Per-clip operations (reframe, captions) do not change job status.
    burn_captions is intentionally skipped — it's slow and manual review
    of captions.ass is recommended before burn-in.
    """

    # --- Stage 1: Transcribe ------------------------------------------------
    try:
        logger.info("[pipeline] %s — transcribe", job_id)
        transcribe_video(job_id)
    except Exception as e:
        _fail(job_id, "transcribe", e)
        return

    # --- Stage 2: Highlight detection ---------------------------------------
    try:
        logger.info("[pipeline] %s — detect_highlights", job_id)
        detect_highlights(job_id)
    except Exception as e:
        _fail(job_id, "detect_highlights", e)
        return

    # --- Stage 3: Cut clips -------------------------------------------------
    try:
        logger.info("[pipeline] %s — cut_clips", job_id)
        clips = cut_clips(job_id)
    except Exception as e:
        _fail(job_id, "cut_clips", e)
        return

    # --- Stage 4: Per-clip reframe + captions -------------------------------
    try:
        clip_ids = _load_clip_ids(job_id)
    except Exception as e:
        _fail(job_id, "load_clip_ids", e)
        return

    for clip_id in clip_ids:
        try:
            logger.info("[pipeline] %s — reframe_clip %s", job_id, clip_id)
            reframe_clip(job_id, clip_id)
        except Exception as e:
            _fail(job_id, f"reframe_clip({clip_id})", e)
            return

        try:
            logger.info("[pipeline] %s — generate_captions %s", job_id, clip_id)
            generate_captions(job_id, clip_id)
        except Exception as e:
            _fail(job_id, f"generate_captions({clip_id})", e)
            return

    # --- Stage 5: Score & title (requires status=clips_ready) ---------------
    try:
        logger.info("[pipeline] %s — score_and_title_clips", job_id)
        score_and_title_clips(job_id)
    except Exception as e:
        _fail(job_id, "score_and_title_clips", e)
        return

    logger.info("[pipeline] %s — done", job_id)
