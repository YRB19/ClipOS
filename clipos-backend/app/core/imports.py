"""Original-preserving library imports and durable queued remote downloads."""
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from sqlmodel import Session
from app.config import settings
from app.core.ffprobe import get_video_metadata
from app.core.media import ffmpeg
from app.models.job import Job, engine
from app.models.source_import import SourceImport
from app.models.execution import now

EXTENSIONS = {'.mp4', '.mkv', '.mov', '.webm', '.m4v'}


def library_path(relative):
    if not settings.media_root:
        raise ValueError('Configure MEDIA_ROOT before importing from the local library')
    root = Path(settings.media_root).expanduser().resolve(strict=True)
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in EXTENSIONS:
        raise ValueError('Choose a media file inside MEDIA_ROOT')
    return path


def validate_remote_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or parsed.hostname not in (
        'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be') or parsed.username or parsed.password:
        raise ValueError('Enter a public YouTube video URL')
    return url


def download_source(job_id, db_engine=None):
    db_engine = db_engine or engine
    with Session(db_engine) as session:
        request = session.get(SourceImport, job_id)
        if not request or not request.url:
            raise ValueError('Remote import request is missing')
        url = validate_remote_url(request.url)
        request.status, request.error_message = 'downloading', None
        session.add(request)
        session.commit()
    folder = Path(settings.storage_raw) / job_id
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / 'source.mkv'
    try:
        # No playlists, no shell, bounded network/process time, original codecs retained.
        subprocess.run(['yt-dlp', '--no-playlist', '--socket-timeout', '30', '--retries', '3',
                        '--ffmpeg-location', str(Path(ffmpeg()).parent),
                        '--max-filesize', str(int(settings.max_source_gb*1024**3)),
                        '-f', 'bestvideo+bestaudio/best', '--merge-output-format', 'mkv',
                        '--remux-video', 'mkv', '-o', str(dest), '--', url],
                       check=True, capture_output=True, text=True, timeout=settings.download_timeout_seconds)
        if dest.stat().st_size > settings.max_source_gb*1024**3:
            raise ValueError('Downloaded source exceeds MAX_SOURCE_GB')
        metadata = get_video_metadata(str(dest))
        if metadata['duration'] <= 0:
            raise ValueError('Downloaded media has no positive duration')
        with Session(db_engine) as session:
            job = session.get(Job, job_id)
            job.video_path, job.duration_seconds = str(dest.resolve()), metadata['duration']
            job.status, job.error_message, job.updated_at = 'ingested', None, now()
            request = session.get(SourceImport, job_id)
            request.status, request.error_message = 'ready', None
            session.add(job)
            session.add(request)
            session.commit()
    except Exception as exc:
        with Session(db_engine) as session:
            request = session.get(SourceImport, job_id)
            request.status, request.error_message = 'failed', str(exc)
            session.add(request)
            session.commit()
        raise
