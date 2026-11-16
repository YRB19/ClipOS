from fastapi import APIRouter, Depends, HTTPException
from app.routers.execution_guard import require_idle
from sqlmodel import Session

from app.core.clip_cutter import cut_clips
from app.models.job import Job, get_session

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(require_idle)])


@router.post("/{job_id}/clips")
def trigger_clip_cutting(
    job_id: str,
    session: Session = Depends(get_session),
):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if job.status != "highlights_ready":
        raise HTTPException(
            status_code=409,
            detail=f"Job status is '{job.status}', must be 'highlights_ready' before clip cutting",
        )

    clips = cut_clips(job_id)

    return {
        "job_id": job_id,
        "clips": [{"clip_id": c["clip_id"], "clip_path": c["clip_path"], "duration": c["duration"]} for c in clips],
    }
