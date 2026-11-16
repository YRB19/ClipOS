from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from app.routers.execution_guard import require_idle
from sqlmodel import Session

from app.core.reframe import reframe_clip
from app.models.job import Job, get_session

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(require_idle)])


@router.post("/{job_id}/clips/{clip_id}/reframe")
def trigger_reframe(
    job_id: str,
    clip_id: str,
    session: Session = Depends(get_session),
):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    clip_path = Path(f"./storage/clips/{job_id}/{clip_id}/clip.mp4")
    if not clip_path.exists():
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found for job {job_id}")

    try:
        vertical_path = reframe_clip(job_id, clip_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reframe failed: {e}")

    return {
        "clip_id": clip_id,
        "vertical_path": vertical_path,
    }
