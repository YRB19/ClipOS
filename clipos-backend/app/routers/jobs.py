"""Job management endpoints: listing, detail, automatic pipeline, queue status."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from redis import Redis
from rq import Queue
from rq.job import Job as RqJob
from sqlmodel import Session, select, col

from app.config import settings
from app.core.queue import enqueue_pipeline
from app.models.job import Job, engine, get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _redis() -> Redis:
    return Redis.from_url(settings.redis_url)


def _job_dict(job: Job) -> dict:
    from pathlib import Path as _Path
    source_filename = _Path(job.video_path).name if job.video_path else None
    return {
        "job_id": job.job_id,
        "source_filename": source_filename,
        "status": job.status,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "duration_seconds": job.duration_seconds,
        "error_message": job.error_message,
    }


# ── GET /jobs ────────────────────────────────────────────────────────────────


@router.get("")
def list_jobs(
    status: str | None = Query(None, description="Filter by status"),
    session: Session = Depends(get_session),
):
    """List all jobs, most recent first.  Optional ?status= filter."""
    stmt = select(Job).order_by(col(Job.created_at).desc())
    result = session.exec(stmt)
    jobs = list(result.all())

    if status:
        jobs = [j for j in jobs if j.status == status]

    return [_job_dict(j) for j in jobs]


# ── GET /jobs/{job_id} ──────────────────────────────────────────────────────


@router.get("/{job_id}")
def get_job(job_id: str, session: Session = Depends(get_session)):
    """Single job detail with clip_ids if clips exist."""
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    result = _job_dict(job)

    # Attach clip_ids if highlights.json exists (contains clip_ids after cut_clips)
    highlights_path = Path(settings.storage_raw) / job_id / "highlights.json"
    if highlights_path.exists():
        import json

        try:
            highlights = json.loads(highlights_path.read_text())
            result["clip_ids"] = [h["clip_id"] for h in highlights if "clip_id" in h]
        except Exception:
            result["clip_ids"] = []
    else:
        result["clip_ids"] = []

    return result


# ── GET /jobs/{job_id}/highlights ────────────────────────────────────────────


@router.get("/{job_id}/highlights")
def get_highlights(job_id: str, session: Session = Depends(get_session)):
    """Return the full highlights data for a job."""
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    highlights_path = Path(settings.storage_raw) / job_id / "highlights.json"
    if not highlights_path.exists():
        raise HTTPException(status_code=404, detail=f"No highlights data for job {job_id}")

    import json
    try:
        return json.loads(highlights_path.read_text())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read highlights: {e}")


# ── POST /jobs/{job_id}/run ─────────────────────────────────────────────────


@router.post("/{job_id}/run")
def run_job(job_id: str, session: Session = Depends(get_session)):
    """Enqueue the full automatic pipeline for this job."""
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if job.status != "ingested":
        raise HTTPException(
            status_code=409,
            detail=f"Job status is '{job.status}', must be 'ingested' to start the automatic pipeline",
        )

    try:
        rq_job_id = enqueue_pipeline(job_id)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to enqueue pipeline: {e}",
        )

    return {"rq_job_id": rq_job_id, "status": "queued"}


# ── GET /jobs/{job_id}/queue-status ─────────────────────────────────────────


@router.get("/{job_id}/queue_status")
def get_queue_status(job_id: str, session: Session = Depends(get_session)):
    """Return pipeline stage from the DB (source of truth) plus RQ state for debugging.

    Response shape:
        job_id, current_stage, rq_state, error_message, started_at, finished_at
    """
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # current_stage is always the pipeline status from the jobs table
    current_stage = job.status

    # Try to find the RQ job for additional debugging info
    rq_state = "unknown"
    started_at = None
    finished_at = None
    try:
        q = Queue(connection=_redis())
        rq_job = None

        for registry in [q.started_job_registry, q.deferred_job_registry,
                         q.scheduled_job_registry]:
            for jid in registry.get_job_ids():
                rj = RqJob.fetch(jid, connection=_redis())
                if rj and rj.args == (job_id,) and rj.func_name == "app.core.pipeline:run_pipeline":
                    rq_job = rj
                    break
            if rq_job:
                break

        if not rq_job:
            for rj in q.jobs:
                if rj.args == (job_id,) and rj.func_name == "app.core.pipeline:run_pipeline":
                    rq_job = rj
                    break

        if rq_job:
            rq_state = rq_job.get_status()
            started_at = rq_job.created_at.isoformat() if rq_job.created_at else None
            finished_at = rq_job.ended_at.isoformat() if rq_job.ended_at else None
        else:
            # No RQ job found — determine likely RQ state from DB status
            if current_stage == "ingested":
                rq_state = "queued"
            elif current_stage in ("done", "failed"):
                rq_state = "finished"
            else:
                rq_state = "started"

    except Exception as e:
        logger.warning("Error querying RQ for job %s: %s", job_id, e)
        rq_state = "unknown"

    return {
        "job_id": job_id,
        "current_stage": current_stage,
        "rq_state": rq_state,
        "error_message": job.error_message,
        "started_at": started_at,
        "finished_at": finished_at,
    }
