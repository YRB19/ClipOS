from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.core.captions import burn_captions, generate_captions
from app.models.job import Job, get_session

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("/{job_id}/clips/{clip_id}/captions")
def trigger_caption_generation(
    job_id: str,
    clip_id: str,
    session: Session = Depends(get_session),
):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    clip_dir = Path(f"./storage/clips/{job_id}/{clip_id}")
    if not clip_dir.exists():
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found for job {job_id}")

    try:
        captions_path = generate_captions(job_id, clip_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Caption generation failed: {e}")

    return {"captions_path": captions_path}


@router.post("/{job_id}/clips/{clip_id}/burn")
def trigger_caption_burn(
    job_id: str,
    clip_id: str,
    session: Session = Depends(get_session),
):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    clip_dir = Path(f"./storage/clips/{job_id}/{clip_id}")
    if not clip_dir.exists():
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found for job {job_id}")

    try:
        captioned_path = burn_captions(job_id, clip_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Caption burn-in failed: {e}")

    return {"captioned_clip_path": captioned_path}
