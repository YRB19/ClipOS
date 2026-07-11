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
    return {
        "job_id": job.job_id,
        "status": job.status,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "duration_seconds": job.duration_seconds,
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
    """Poll RQ for the underlying rq_job_id's state.

    Looks for an RQ job that was enqueued for this ClipOS job_id.
    Returns the RQ state (queued/started/finished/failed) and metadata.
    """
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    try:
        q = Queue(connection=_redis())
        # Search for RQ jobs by iterating recent job IDs — RQ doesn't have
        # a native "find by args" query, so we check the most recent jobs.
        # For correctness, we store the rq_job_id on the Job model at
        # enqueue time.  Since we don't have a dedicated field, we scan
        # the queue's started/queued/finished registries.
        rq_job = None

        # Check all active registries for a job with matching args
        for registry in [q.started_job_registry, q.deferred_job_registry,
                         q.scheduled_job_registry]:
            for jid in registry.get_job_ids():
                rj = RqJob.fetch(jid, connection=_redis())
                if rj and rj.args == (job_id,) and rj.func_name == "app.core.pipeline:run_pipeline":
                    rq_job = rj
                    break
            if rq_job:
                break

        # Also check the main queue
        if not rq_job:
            for rj in q.jobs:
                if rj.args == (job_id,) and rj.func_name == "app.core.pipeline:run_pipeline":
                    rq_job = rj
                    break

        if not rq_job:
            return {"rq_job_id": None, "state": "not_found", "detail": "No RQ job found for this ClipOS job"}

        return {
            "rq_job_id": rq_job.id,
            "state": rq_job.get_status(),
            "created_at": rq_job.created_at.isoformat() if rq_job.created_at else None,
            "ended_at": rq_job.ended_at.isoformat() if rq_job.ended_at else None,
            "result": str(rq_job.return_value) if rq_job.return_value is not None else None,
            "exc_info": rq_job.exc_info if rq_job.exc_info else None,
        }
    except Exception as e:
        logger.warning("Error querying RQ for job %s: %s", job_id, e)
        return {"rq_job_id": None, "state": "unknown", "detail": str(e)}
