from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.config import settings
from app.core.transcribe import transcribe_video
from app.models.job import Job, get_session

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("/{job_id}/transcribe")
def trigger_transcribe(
    job_id: str,
    session: Session = Depends(get_session),
):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if job.status != "ingested":
        raise HTTPException(
            status_code=409,
            detail=f"Job status is '{job.status}', must be 'ingested' before transcribing",
        )

    transcript = transcribe_video(job_id)

    transcript_path = str(Path(settings.storage_raw) / job_id / "transcript.json")

    return {
        "job_id": job_id,
        "word_count": len(transcript),
        "duration_covered": transcript[-1]["end"] if transcript else 0.0,
        "transcript_path": transcript_path,
    }
