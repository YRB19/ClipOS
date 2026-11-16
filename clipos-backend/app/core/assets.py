"""Source identity/probing and reusable low-resolution navigation assets."""
import array
import hashlib
import json
import math
import subprocess
import tempfile
import wave
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session
from app.config import settings
from app.core.artifacts import write_json
from app.core.ffprobe import get_video_metadata
from app.core.media import ffmpeg
from app.models.job import Job, engine
from app.models.edit import SourceAsset
from app.models.execution import now

ASSET_VERSION = 'navigation-v1'


def ensure_source(job_id, db_engine=None):
    db_engine = db_engine or engine
    with Session(db_engine) as session:
        job = session.get(Job, job_id)
        if not job or not job.video_path:
            raise FileNotFoundError('Source job/media not found')
        path = Path(job.video_path).resolve()
        stat = path.stat()
        existing = session.get(SourceAsset, job_id)
        if existing and (existing.source_path, existing.source_size, existing.source_mtime_ns) == (
                str(path), stat.st_size, stat.st_mtime_ns):
            session.expunge(existing)
            return existing
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(4*1024*1024):
            digest.update(chunk)
    metadata = get_video_metadata(str(path))
    if metadata['duration'] <= 0:
        raise ValueError('Source has no positive duration')
    after = path.stat()
    if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Source changed during fingerprinting; retry after import finishes')
    with Session(db_engine) as session:
        asset = session.get(SourceAsset, job_id) or SourceAsset(
            job_id=job_id, fingerprint='', source_path='', source_size=0, source_mtime_ns=0, metadata_json='{}')
        asset.fingerprint, asset.source_path = digest.hexdigest(), str(path)
        asset.source_size, asset.source_mtime_ns = stat.st_size, stat.st_mtime_ns
        asset.metadata_json, asset.artifacts_json = json.dumps(metadata), '{}'
        asset.status, asset.error_message, asset.updated_at = 'probed', None, now()
        session.add(asset)
        try:
            session.commit()
        except IntegrityError:
            # Concurrent initial reads can fingerprint the same immutable job.
            session.rollback()
            asset = session.get(SourceAsset, job_id)
            if not asset or asset.fingerprint != digest.hexdigest():
                raise ValueError('Source registration changed concurrently; retry')
        session.refresh(asset)
        session.expunge(asset)
        return asset


def source_unchanged(asset):
    stat = Path(asset.source_path).stat()
    if (stat.st_size, stat.st_mtime_ns) != (asset.source_size, asset.source_mtime_ns):
        raise ValueError('Original source changed while processing; output was not published')


def artifact_stamp(path):
    stat = Path(path).stat()
    return f'{stat.st_size}:{stat.st_mtime_ns}'


def assets_valid(asset):
    try:
        artifacts = json.loads(asset.artifacts_json)
        return asset.status == 'ready' and artifacts['version'] == ASSET_VERSION and all(
            artifact_stamp(item['path']) == item['signature']
            for item in artifacts['files'].values()) and len(artifacts['files']) == 3
    except (OSError, KeyError, ValueError, TypeError):
        return False


def prepare_assets(job_id, db_engine=None):
    db_engine = db_engine or engine
    asset = ensure_source(job_id, db_engine)
    if assets_valid(asset):
        return json.loads(asset.artifacts_json)
    folder = Path(settings.storage_raw)/job_id/'assets'/asset.fingerprint/ASSET_VERSION
    folder.mkdir(parents=True, exist_ok=True)
    meta = json.loads(asset.metadata_json)
    def run(args):
        return subprocess.run([ffmpeg(), '-y', '-v', 'error', *args], check=True,
                              capture_output=True, timeout=settings.media_timeout_seconds)
    try:
        with Session(db_engine) as session:
            current = session.get(SourceAsset, job_id)
            current.status = 'preparing'
            session.add(current)
            session.commit()
        # A private directory prevents partial outputs from ever being advertised.
        with tempfile.TemporaryDirectory(dir=folder) as temporary:
            temp = Path(temporary)
            proxy = temp/'proxy.mp4'
            args = ['-i', asset.source_path, '-map', f"0:{meta['video_stream_index']}"]
            if meta['audio_tracks']:
                args += ['-map', f"0:{meta['audio_tracks'][0]['stream_index']}"]
            args += ['-vf', 'scale=trunc(iw*sar/2)*2:ih,setsar=1,scale=960:540:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1',
                     '-r', '30', '-c:v', 'libx264', '-crf', '28', '-preset', 'fast', '-pix_fmt', 'yuv420p',
                     '-c:a', 'aac', '-ac', '2', '-b:a', '96k', '-movflags', '+faststart', str(proxy)]
            run(args)
            if get_video_metadata(str(proxy))['duration'] <= 0:
                raise ValueError('Invalid proxy output')
            thumb = temp/'thumbnail.jpg'
            run(['-ss', str(min(5, meta['duration']/10)), '-i', asset.source_path,
                 '-map', f"0:{meta['video_stream_index']}", '-frames:v', '1', '-vf', 'scale=trunc(iw*sar/2)*2:ih,setsar=1,scale=480:-2', str(thumb)])
            if not thumb.is_file() or not thumb.stat().st_size:
                raise ValueError('No thumbnail produced')
            peaks, interval = [], meta['duration']/6000
            track = meta['audio_tracks'][0]['stream_index'] if meta['audio_tracks'] else None
            if track is not None:
                audio = temp/'waveform.wav'
                run(['-i', asset.source_path, '-map', f'0:{track}', '-vn', '-ac', '1', '-ar', '8000',
                     '-c:a', 'pcm_s16le', str(audio)])
                with wave.open(str(audio), 'rb') as stream:
                    block = max(1, math.ceil(stream.getnframes()/6000))
                    interval = block/stream.getframerate()
                    while chunk := stream.readframes(block):
                        samples = array.array('h', chunk)
                        peaks.append(round(max((abs(n) for n in samples), default=0)/32768, 4))
            write_json(temp/'waveform.json', dict(peaks=peaks, interval_seconds=interval,
                                                  duration=meta['duration'], stream_index=track))
            files = {}
            for name in ('proxy.mp4', 'thumbnail.jpg', 'waveform.json'):
                destination = folder/name
                (temp/name).replace(destination)
                files[name] = dict(path=str(destination.resolve()), signature=artifact_stamp(destination))
        source_unchanged(asset)
        artifacts = dict(version=ASSET_VERSION, fingerprint=asset.fingerprint, files=files)
        with Session(db_engine) as session:
            current = session.get(SourceAsset, job_id)
            if current.fingerprint != asset.fingerprint:
                raise ValueError('Source changed while preparing navigation assets')
            current.status, current.artifacts_json, current.updated_at = 'ready', json.dumps(artifacts), now()
            current.error_message = None
            session.add(current)
            session.commit()
        return artifacts
    except Exception as exc:
        with Session(db_engine) as session:
            current = session.get(SourceAsset, job_id)
            current.status, current.error_message, current.updated_at = 'failed', str(exc), now()
            session.add(current)
            session.commit()
        raise
