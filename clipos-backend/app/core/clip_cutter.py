import json
import logging
import subprocess
from pathlib import Path

from app.config import settings
from app.core.ffprobe import get_video_metadata
from app.models.job import Job, engine
from sqlmodel import Session

logger = logging.getLogger(__name__)


def cut_clips(job_id: str) -> list[dict]:
    """Load highlights.json, extract each candidate segment from source.mp4 via ffmpeg."""
    raw_dir = Path(settings.storage_raw) / job_id
    clips_dir = Path(settings.storage_clips) / job_id
    source_path = raw_dir / "source.mp4"
    highlights_path = raw_dir / "highlights.json"

    if not source_path.exists():
        raise FileNotFoundError(f"No source.mp4 found for job {job_id}")
    if not highlights_path.exists():
        raise FileNotFoundError(f"No highlights.json found for job {job_id}")

    highlights = json.loads(highlights_path.read_text())
    if not highlights:
        raise ValueError(f"Highlights list is empty for job {job_id}")

    # Get source duration for clamping
    source_meta = get_video_metadata(str(source_path))
    source_duration = source_meta["duration"]

    results = []
    errors = []

    for candidate in highlights:
        clip_id = candidate["clip_id"]
        start = float(candidate["start"])
        end = float(candidate["end"])

        # Clamp to video bounds
        if start < 0:
            logger.warning("Clamp start %.2f → 0 for clip %s", start, clip_id)
            start = 0.0
        if end > source_duration:
            logger.warning("Clamp end %.2f → %.2f for clip %s", end, source_duration, clip_id)
            end = source_duration
        if start >= end:
            logger.warning("Skipping clip %s: start %.2f >= end %.2f", clip_id, start, end)
            errors.append({"clip_id": clip_id, "error": f"Invalid range: start={start}, end={end}"})
            continue

        clip_dir = clips_dir / clip_id
        clip_dir.mkdir(parents=True, exist_ok=True)
        clip_path = clip_dir / "clip.mp4"

        try:
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-i", str(source_path),
                "-t", str(end - start),
                "-c:v", "libx264",
                "-c:a", "aac",
                "-preset", "fast",
                str(clip_path),
            ]
            subprocess.run(cmd, check=True, capture_output=True, text=True)

            if not clip_path.exists():
                raise RuntimeError(f"ffmpeg completed but output not found: {clip_path}")

            clip_meta = get_video_metadata(str(clip_path))

            results.append({
                "clip_id": clip_id,
                "clip_path": str(clip_path),
                "duration": clip_meta["duration"],
            })

        except Exception as e:
            logger.error("Failed to cut clip %s: %s", clip_id, e)
            errors.append({"clip_id": clip_id, "error": str(e)})

    # Update job status
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job:
            from datetime import datetime, timezone
            job.updated_at = datetime.now(timezone.utc)
            if results:
                job.status = "clips_ready"
            else:
                job.status = "failed"
                job.error_message = f"All {len(errors)} clips failed: {errors[0]['error']}" if errors else "No clips produced"
            session.add(job)
            session.commit()

    return results
