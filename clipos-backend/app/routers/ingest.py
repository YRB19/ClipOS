import os
import subprocess
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlmodel import Session

from app.config import settings
from app.core.ffprobe import get_video_metadata
from app.models.job import Job, get_session

router = APIRouter(prefix="/ingest", tags=["ingest"])

ALLOWED_EXTENSIONS = {".mp4"}


@router.post("/upload")
def ingest_upload(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Invalid file type '{ext}'. Only .mp4 is accepted.")

    job_id = str(uuid.uuid4())
    job_dir = Path(settings.storage_raw) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    dest = job_dir / "source.mp4"

    try:
        with open(dest, "wb") as f:
            while chunk := file.file.read(1024 * 1024):
                f.write(chunk)

        metadata = get_video_metadata(str(dest))

        job = Job(
            job_id=job_id,
            source_type="upload",
            status="ingested",
            video_path=str(dest),
            duration_seconds=metadata["duration"],
        )
        session.add(job)
        session.commit()
        session.refresh(job)

        return {"job_id": job_id}

    except Exception as e:
        session.rollback()
        # Clean up partial file
        if dest.exists():
            dest.unlink()
        job = Job(
            job_id=job_id,
            source_type="upload",
            status="failed",
            video_path="",
            duration_seconds=0.0,
            error_message=str(e),
        )
        session.add(job)
        session.commit()
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {e}")


@router.post("/youtube")
def ingest_youtube(
    body: dict,
    session: Session = Depends(get_session),
):
    url = body.get("url")
    if not url:
        raise HTTPException(status_code=422, detail="Missing 'url' field in request body")

    job_id = str(uuid.uuid4())
    job_dir = Path(settings.storage_raw) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    dest = job_dir / "source.mp4"

    try:
        cmd = [
            "yt-dlp",
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "-o", str(dest),
            url,
        ]
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)

        if not dest.exists():
            raise RuntimeError("yt-dlp completed but output file not found")

        metadata = get_video_metadata(str(dest))

        job = Job(
            job_id=job_id,
            source_type="youtube",
            status="ingested",
            video_path=str(dest),
            duration_seconds=metadata["duration"],
        )
        session.add(job)
        session.commit()
        session.refresh(job)

        return {"job_id": job_id}

    except subprocess.CalledProcessError as e:
        session.rollback()
        if dest.exists():
            dest.unlink()
        error_msg = e.stderr.strip() if e.stderr else str(e)
        job = Job(
            job_id=job_id,
            source_type="youtube",
            status="failed",
            video_path="",
            duration_seconds=0.0,
            error_message=error_msg,
        )
        session.add(job)
        session.commit()
        raise HTTPException(status_code=422, detail=f"YouTube download failed: {error_msg}")

    except Exception as e:
        session.rollback()
        if dest.exists():
            dest.unlink()
        job = Job(
            job_id=job_id,
            source_type="youtube",
            status="failed",
            video_path="",
            duration_seconds=0.0,
            error_message=str(e),
        )
        session.add(job)
        session.commit()
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {e}")
