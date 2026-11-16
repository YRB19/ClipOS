import json
import logging
import subprocess
from pathlib import Path

from app.config import settings
from app.core.media import ffmpeg
from app.core.ffprobe import get_video_metadata
from app.core.artifacts import write_json
from app.models.job import Job, engine
from sqlmodel import Session

logger = logging.getLogger(__name__)


def cut_clip(job_id: str, clip_id: str) -> dict:
    """Cut a single candidate. Raise failures so the queue can account for them."""
    raw_dir = Path(settings.storage_raw) / job_id
    with Session(engine) as session:
        job = session.get(Job, job_id)
        source_path = Path(job.video_path) if job else raw_dir / 'source.mp4'
    highlights_path = raw_dir / 'highlights.json'
    highlights = json.loads(highlights_path.read_text())
    candidate = next(h for h in highlights if h['clip_id'] == clip_id)
    duration = get_video_metadata(str(source_path))['duration']
    start, end = max(0.0, float(candidate['start'])), min(duration, float(candidate['end']))
    if not 0 <= start < end <= duration:
        raise ValueError(f'Invalid range: start={start}, end={end}')
    # Captions and retries must use exactly the same clamped source interval.
    candidate.update(start=start, end=end)
    write_json(highlights_path, highlights)
    clip_dir = Path(settings.storage_clips) / job_id / clip_id
    clip_dir.mkdir(parents=True, exist_ok=True)
    output = clip_dir / 'clip.mp4'
    temporary = clip_dir / 'clip.partial.mp4'
    try:
        subprocess.run([ffmpeg(), '-y', '-ss', str(start), '-i', str(source_path),
                        '-t', str(end-start), '-c:v', 'libx264', '-c:a', 'aac',
                        '-preset', 'fast', str(temporary)], check=True,
                       capture_output=True, text=True, timeout=settings.media_timeout_seconds)
        meta = get_video_metadata(str(temporary))
        if meta['duration'] <= 0:
            raise RuntimeError('Cut output has zero duration')
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return dict(clip_id=clip_id, clip_path=str(output), duration=meta['duration'])


def cut_clips(job_id: str) -> list[dict]:
    """Compatibility batch API; automatic processing queues individual cuts."""
    highlights = json.loads((Path(settings.storage_raw) / job_id / 'highlights.json').read_text())
    if not highlights:
        raise ValueError(f'Highlights list is empty for job {job_id}')
    results, errors = [], []
    for candidate in highlights:
        try:
            results.append(cut_clip(job_id, candidate['clip_id']))
        except Exception as exc:
            logger.exception('Failed to cut clip %s', candidate['clip_id'])
            errors.append(str(exc))
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job:
            from app.models.execution import now
            job.updated_at = now()
            job.status = 'clips_ready' if results else 'failed'
            job.error_message = None if results else '; '.join(errors)
            session.add(job)
            session.commit()
    return results
