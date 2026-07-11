from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.core.scoring import score_and_title_clips
from app.models.job import Job, get_session

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("/{job_id}/score")
def trigger_scoring(
    job_id: str,
    session: Session = Depends(get_session),
):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if job.status != "clips_ready":
        raise HTTPException(
            status_code=409,
            detail=f"Job status is '{job.status}', must be 'clips_ready' before scoring",
        )

    try:
        scored = score_and_title_clips(job_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scoring failed: {e}")

    return scored
