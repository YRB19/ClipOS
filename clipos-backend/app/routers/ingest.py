import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlmodel import Session

from app.config import settings
from app.core.ffprobe import get_video_metadata
from app.core.imports import EXTENSIONS, library_path, validate_remote_url
from app.models.source_import import SourceImport
from app.models.job import Job, get_session

router = APIRouter(prefix="/ingest", tags=["ingest"])

ALLOWED_EXTENSIONS = EXTENSIONS


@router.post("/upload")
def ingest_upload(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Invalid file type '{ext}'. Supported: MP4, MKV, MOV, WebM, M4V.")

    job_id = str(uuid.uuid4())
    job_dir = Path(settings.storage_raw) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    dest = job_dir / f"source{ext}"

    try:
        size = 0
        with open(dest, "wb") as f:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_source_gb * 1024**3:
                    raise HTTPException(status_code=413, detail="Upload exceeds MAX_SOURCE_GB")
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
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {e}")


@router.get("/library")
def list_library():
    if not settings.media_root:
        return {"configured": False, "files": []}
    root = Path(settings.media_root).expanduser().resolve()
    files = []
    # Bound discovery for large libraries; revalidate symlinks at import time.
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in EXTENSIONS and path.resolve().is_relative_to(root):
            files.append(str(path.relative_to(root)))
            if len(files) >= 1000:
                break
    return {"configured": True, "files": sorted(files)}


@router.post("/local")
def ingest_local(body: dict, session: Session = Depends(get_session)):
    try:
        path = library_path(str(body.get("path", "")))
        metadata = get_video_metadata(str(path))
        track = body.get("audio_stream_index")
        if track is not None and (type(track) is not int or track not in [t['stream_index'] for t in metadata['audio_tracks']]):
            raise ValueError('Selected microphone stream does not exist')
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    job = Job(job_id=str(uuid.uuid4()), source_type="local", status="ingested",
              video_path=str(path), duration_seconds=metadata['duration'])
    session.add(job)
    session.add(SourceImport(job_id=job.job_id, kind='local', audio_stream_index=track))
    session.commit()
    return {"job_id": job.job_id}


@router.post('/local/probe')
def probe_local(body: dict):
    try:
        return get_video_metadata(str(library_path(str(body.get('path', '')))))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/youtube", status_code=202)
def ingest_youtube(body: dict, session: Session = Depends(get_session)):
    try:
        url = validate_remote_url(str(body.get('url', '')))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    job = Job(job_id=str(uuid.uuid4()), source_type="youtube", status="ingested",
              video_path="", duration_seconds=0)
    session.add(job)
    session.add(SourceImport(job_id=job.job_id, kind='remote', url=url, status='queued'))
    session.commit()
    # /run persists the queue claim. A failed dispatch remains resumable by job ID.
    from app.routers.jobs import run_job
    try:
        run_job(job.job_id, session)
    except HTTPException:
        pass
    session.expire_all()
    request = session.get(SourceImport, job.job_id)
    return {"job_id": job.job_id, "import_status": request.status,
            "error_message": request.error_message}
