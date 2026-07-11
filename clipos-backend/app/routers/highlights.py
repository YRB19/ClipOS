from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.config import settings
from app.core.highlights import detect_highlights
from app.models.job import Job, get_session

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("/{job_id}/highlights")
def trigger_highlights(
    job_id: str,
    session: Session = Depends(get_session),
):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if job.status != "transcribed":
        raise HTTPException(
            status_code=409,
            detail=f"Job status is '{job.status}', must be 'transcribed' before highlight detection",
        )

    try:
        highlights = detect_highlights(job_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Highlight detection failed: {e}")

    highlights_path = str(Path(settings.storage_raw) / job_id / "highlights.json")

    return {
        "job_id": job_id,
        "candidate_count": len(highlights),
        "highlights_path": highlights_path,
    }
