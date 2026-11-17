"""Durable sequential work units. Each queue task has a bounded deadline and checkpoint."""
import hashlib
import json
import logging
import threading
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

import requests
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config import settings
from app.models.job import Job, engine
from app.models.execution import PipelineRun, StageAttempt, ClipState, now

logger = logging.getLogger(__name__)
INITIAL_STAGES = ('assets', 'transcribe_chunk', 'highlights_chunk')
CLIP_STAGES = ('recipe', 'render')


def _attempts(session, job_id):
    return list(session.exec(select(StageAttempt).where(StageAttempt.job_id == job_id)
                            .order_by(StageAttempt.created_at.desc())).all())


def output_path(job_id, stage, clip_id=None):
    if stage in ('transcribe', 'highlights', 'score'):
        return Path(settings.storage_raw) / job_id / ('transcript.json' if stage == 'transcribe' else 'highlights.json')
    name = dict(cut='clip.mp4', reframe='clip_vertical.mp4', captions='captions.ass', burn='clip_captioned.mp4')[stage]
    return Path(settings.storage_clips) / job_id / clip_id / name


def signature(job_id, stage, clip_id=None):
    if stage == 'rank':
        from app.core.selection import rank_signature
        return rank_signature(job_id, engine)
    if stage == 'assess':
        from app.core.selection import assessment, digest
        result = assessment(job_id, clip_id, engine)
        if result is None:
            raise ValueError('Visual assessment is missing')
        return digest(result)
    if stage.endswith('_chunk'):
        from app.core.chunks import checkpoint_signature
        return checkpoint_signature(job_id, stage, clip_id, engine)
    if stage == 'download':
        with Session(engine) as session:
            job = session.get(Job, job_id)
            path = Path(job.video_path)
        return _media_signature(str(path), path.stat().st_size, path.stat().st_mtime_ns, 'download')
    if stage == 'assets':
        from app.core.assets import assets_valid, source_unchanged
        from app.models.edit import SourceAsset
        with Session(engine) as session:
            asset = session.get(SourceAsset, job_id)
            job = session.get(Job, job_id)
            if not asset or asset.source_path != str(Path(job.video_path).resolve()):
                raise ValueError('Source assets are missing or changed')
            source_unchanged(asset)
        if not assets_valid(asset):
            raise ValueError('Navigation assets are incomplete')
        return hashlib.sha256((asset.fingerprint+asset.artifacts_json).encode()).hexdigest()
    if stage == 'recipe':
        from app.core.recipes import read_edit
        edit = read_edit(job_id, clip_id, engine)
        if not edit:
            raise ValueError('Clip edit is missing')
        return edit['revision_id']
    if stage == 'render':
        from app.core.renders import final_output
        output = final_output(job_id, clip_id, engine)
        if not output:
            raise ValueError('Current final render is missing')
        return _media_signature(output.output_path, Path(output.output_path).stat().st_size,
                                Path(output.output_path).stat().st_mtime_ns, 'render')
    path = output_path(job_id, stage, clip_id)
    if path.suffix == '.json':
        data = json.loads(path.read_text())
        if not isinstance(data, list) or (not data and stage not in ('transcribe', 'highlights', 'score')):
            raise ValueError(f'Empty or invalid {path.name}')
        if stage == 'transcribe':
            for word in data:
                if not isinstance(word['word'], str) or not 0 <= word['start'] < word['end']:
                    raise ValueError('Invalid transcript checkpoint')
        else:
            for highlight in data:
                if not highlight.get('clip_id') or not 0 <= highlight['start'] < highlight['end']:
                    raise ValueError('Invalid highlight checkpoint')
        # Later cutting/scoring add metadata, reorder and clamp highlights. Identity
        # is intentionally based on IDs, not mutable ranking/trim fields.
        content = json.dumps(sorted(h['clip_id'] for h in data)) if stage == 'highlights' else json.dumps(data, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()
    if path.suffix == '.ass':
        content = path.read_text()
        if '[Events]' not in content:
            raise ValueError('Invalid ASS checkpoint')
        return hashlib.sha256(content.encode()).hexdigest()
    stat = path.stat()
    return _media_signature(str(path), stat.st_size, stat.st_mtime_ns, stage)


@lru_cache(maxsize=256)
def _media_signature(path, size, mtime, stage):
    from app.core.ffprobe import get_video_metadata
    meta = get_video_metadata(path)
    if meta['duration'] <= 0 or meta['width'] <= 0:
        raise ValueError('Invalid media checkpoint')
    if stage in ('reframe', 'burn', 'render') and (meta['width'], meta['height']) != (1080, 1920):
        raise ValueError('Invalid vertical geometry')
    return f'{size}:{mtime}'


def input_signature(job_id, stage, clip_id=None):
    if stage == 'rank':
        from app.core.selection import rank_key
        return rank_key(job_id, engine)
    if stage == 'assess':
        from app.core.selection import assessment_key
        return assessment_key(job_id, clip_id, engine)
    if stage.endswith('_chunk'):
        from app.core.chunks import context
        return context(job_id, stage, clip_id, engine)[2]
    if stage == 'download':
        from app.models.source_import import SourceImport
        with Session(engine) as session:
            return hashlib.sha256(session.get(SourceImport, job_id).url.encode()).hexdigest()
    with Session(engine) as session:
        job = session.get(Job, job_id)
        source = Path(job.video_path)
    stat = source.stat()
    source_id = f'{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}'
    if stage in ('assets', 'transcribe'):
        return source_id
    if stage == 'highlights':
        return signature(job_id, 'transcribe')
    if stage in ('cut', 'captions', 'score', 'recipe'):
        highlights = json.loads(output_path(job_id, 'highlights').read_text())
        ranges = sorted((h['clip_id'], h['start'], h['end']) for h in highlights
                        if clip_id is None or h['clip_id'] == clip_id)
        inputs = [source_id, ranges]
        if stage in ('captions', 'score', 'recipe'):
            words = json.loads(output_path(job_id, 'transcribe').read_text())
            if clip_id:
                words = [w for w in words if any(w['end'] > start and w['start'] < end for _, start, end in ranges)]
            inputs.append(words)
        return hashlib.sha256(json.dumps(inputs).encode()).hexdigest()
    if stage == 'render':
        from app.core.renderer import RENDERER_VERSION
        from app.core.assets import ensure_source
        return signature(job_id, 'recipe', clip_id) + ensure_source(job_id, engine).fingerprint + RENDERER_VERSION
    if stage == 'reframe':
        return signature(job_id, 'cut', clip_id)
    return signature(job_id, 'reframe', clip_id) + signature(job_id, 'captions', clip_id)


def _valid(attempt):
    try:
        return (attempt.status == 'completed' and attempt.input_signature == input_signature(
            attempt.job_id, attempt.stage, attempt.clip_id) and attempt.output_signature == signature(
            attempt.job_id, attempt.stage, attempt.clip_id))
    except Exception:
        return False


def _plan(job_id, attempts, db_engine=None):
    db_engine = db_engine or engine
    if 'transcribe_chunk' in INITIAL_STAGES:
        from app.core.chunks import windows, load
        from app.models.source_import import SourceImport
        with Session(db_engine) as session:
            job = session.get(Job, job_id)
            request = session.get(SourceImport, job_id)
            remote = request and request.kind == 'remote'
        units = [('download', None)] if remote else []
        if not job.video_path:
            return units
        units.append(('assets', None))
        latest_assets = next((a for a in attempts if a.stage == 'assets'), None)
        # Fingerprinting/probing stay in the worker, never the initial /run request.
        if not latest_assets or not _valid(latest_assets):
            return units
        from app.core.selection import shortlist
        latest = {}
        for attempt in attempts:
            latest.setdefault(attempt.unit_key, attempt)
        all_analyzed = True
        for window in windows(job.duration_seconds):
            index = str(window['index'])
            for stage in ('transcribe_chunk', 'highlights_chunk'):
                units.append((stage, index))
                attempt = latest.get(_key(stage, index))
                if not attempt or not _valid(attempt):
                    all_analyzed = False
        if not all_analyzed:
            return units
        if settings.selection_vision_enabled:
            units.extend(('assess', h['clip_id']) for h in shortlist(job_id, db_engine))
        units.append(('rank', None))
        rank = latest.get('rank')
        if rank and _valid(rank):
            candidates = json.loads((Path(settings.storage_raw)/job_id/'highlights.json').read_text())
            for h in candidates:
                units.extend((stage, h['clip_id']) for stage in CLIP_STAGES)
        return units
    units = [(stage, None) for stage in INITIAL_STAGES]
    # Only expand candidates after a verified detection checkpoint. Artifacts from
    # a legacy/manual run are preserved but never silently treated as verified.
    latest_detection = next((a for a in attempts if a.stage == 'highlights'), None)
    if latest_detection and _valid(latest_detection):
        highlights = json.loads(output_path(job_id, 'highlights').read_text())
        for h in highlights:
            units.extend((stage, h['clip_id']) for stage in CLIP_STAGES)
        units.append(('score', None))
    return units


def _restore_aggregates(job_id, db_engine):
    """Missing/corrupt aggregates can be rebuilt without repeating valid inference."""
    from app.core.chunks import assemble_transcript, assemble_candidates
    for filename, assemble, required in (
        ('transcript.json', assemble_transcript, {'word', 'start', 'end'}),
        ('candidates.json', assemble_candidates, {'clip_id', 'start', 'end'}),
        ('highlights.json', assemble_candidates, {'clip_id', 'start', 'end'})):
        try:
            data = json.loads((Path(settings.storage_raw)/job_id/filename).read_text())
            if not isinstance(data, list) or any(not isinstance(item, dict) or not required.issubset(item) for item in data):
                raise ValueError('Invalid aggregate')
        except (OSError, ValueError):
            assemble(job_id, db_engine)


def _key(stage, clip_id):
    return f'{stage}:{clip_id}' if clip_id else stage


def _finish(session, run, status, message=None):
    run.status, run.active_key, run.current_task_id = status, None, None
    run.finished_at, run.error_message = now(), message
    job = session.get(Job, run.job_id)
    job.status = 'done' if status == 'completed' else 'failed'
    job.error_message, job.updated_at = message, now()
    session.add(job)
    session.add(run)
    if run.current_stage == 'download' and status in ('failed', 'cancelled'):
        from app.models.source_import import SourceImport
        request = session.get(SourceImport, run.job_id)
        if request:
            request.status, request.error_message = status, message
            session.add(request)


def claim_run(job_id, db_engine=None):
    """UNIQUE active_key makes simultaneous start/resume requests idempotent."""
    db_engine = db_engine or engine
    with Session(db_engine) as session:
        active = session.exec(select(PipelineRun).where(PipelineRun.active_key == job_id)).first()
        if active:
            return active, False
        run = PipelineRun(run_id=str(uuid4()), job_id=job_id, active_key=job_id)
        session.add(run)
        try:
            job = session.get(Job, job_id)
            job.error_message = None
            job.updated_at = now()
            session.add(job)
            session.commit()
            session.refresh(run)
            return run, True
        except IntegrityError:
            session.rollback()
            return session.exec(select(PipelineRun).where(PipelineRun.active_key == job_id)).one(), False


def dispatch_next(run_id, db_engine=None):
    """Commit a task ID before enqueueing; reconciliation closes either crash gap."""
    db_engine = db_engine or engine
    with Session(db_engine) as session:
        run = session.get(PipelineRun, run_id)
        if not run or not run.active_key or run.current_task_id:
            return
        if run.cancel_requested:
            _finish(session, run, 'cancelled', 'Cancelled at a work-unit boundary')
            session.commit()
            return
        attempts = _attempts(session, run.job_id)
        if 'transcribe_chunk' in INITIAL_STAGES and any(a.stage.endswith('_chunk') and a.status == 'completed' for a in attempts):
            latest_assets = next((a for a in attempts if a.stage == 'assets'), None)
            if latest_assets and _valid(latest_assets):
                _restore_aggregates(run.job_id, db_engine)
        failed_clips = {c.clip_id for c in session.exec(select(ClipState).where(
            ClipState.run_id == run_id, ClipState.status == 'failed')).all()}
        for stage, clip_id in _plan(run.job_id, attempts, db_engine):
            if stage in CLIP_STAGES and clip_id in failed_clips:
                continue
            unit_key = _key(stage, clip_id)
            matching = [a for a in attempts if a.unit_key == unit_key]
            if matching and _valid(matching[0]):
                continue
            task_id = str(uuid4())
            claimed = session.execute(update(PipelineRun).where(
                PipelineRun.run_id == run_id, PipelineRun.current_task_id.is_(None),
                PipelineRun.active_key.is_not(None)).values(
                    current_task_id=task_id, current_stage=stage, status='queued', heartbeat_at=now()))
            if not claimed.rowcount:
                session.rollback()
                return
            attempt = StageAttempt(attempt_id=task_id, rq_task_id=task_id, run_id=run_id,
                                   job_id=run.job_id, stage=stage, clip_id=clip_id, unit_key=unit_key)
            session.add(attempt)
            session.commit()
            # A retry is a new RQ task and durable attempt, leaving the failed
            # attempt in both RQ's failed registry and the database.
            failures = sum(a.run_id == run_id and a.unit_key == unit_key and a.status == 'failed' for a in attempts)
            try:
                from app.core.queue import enqueue_stage
                enqueue_stage(task_id, stage, delay=10 * failures)
            except Exception as exc:
                # Ambiguous enqueue acknowledgements are fenced: an accidentally
                # delivered task sees an inactive run and cannot start.
                with Session(db_engine) as recovery:
                    a = recovery.get(StageAttempt, task_id)
                    r = recovery.get(PipelineRun, run_id)
                    if r.current_task_id == task_id and a.status == 'queued':
                        a.status, a.error_message, a.finished_at = 'failed', f'Enqueue failed: {exc}', now()
                        recovery.add(a)
                        _finish(recovery, r, 'failed', a.error_message)
                        recovery.commit()
                raise
            return
        failed = list(failed_clips)
        _finish(session, run, 'completed_with_errors' if failed else 'completed',
                f'{len(failed)} clip(s) failed; successful clips remain available' if failed else None)
        session.commit()


@contextmanager
def _heartbeat(run_id):
    stop = threading.Event()
    def pulse():
        while not stop.wait(settings.heartbeat_seconds):
            try:
                with Session(engine) as session:
                    session.execute(update(PipelineRun).where(
                        PipelineRun.run_id == run_id, PipelineRun.active_key.is_not(None)).values(heartbeat_at=now()))
                    session.commit()
            except Exception:
                logger.exception('Heartbeat failed for %s', run_id)
    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)


def _execute(attempt):
    # Lazy imports avoid loading inference/video libraries in API processes.
    job_id, clip_id = attempt.job_id, attempt.clip_id
    if attempt.stage == 'rank':
        from app.core.selection import rank_candidates
        return rank_candidates(job_id)
    if attempt.stage == 'assess':
        from app.core.selection import assess_candidate
        return assess_candidate(job_id, clip_id)
    if attempt.stage == 'download':
        from app.core.imports import download_source
        return download_source(job_id)
    if attempt.stage.endswith('_chunk'):
        from app.core.chunks import transcribe_chunk, highlights_chunk
        return (transcribe_chunk if attempt.stage == 'transcribe_chunk' else highlights_chunk)(job_id, clip_id)
    if attempt.stage == 'assets':
        from app.core.assets import prepare_assets
        return prepare_assets(job_id)
    if attempt.stage == 'recipe':
        from app.core.recipes import ensure_edit
        return ensure_edit(job_id, clip_id)
    if attempt.stage == 'render':
        from app.core.renders import render_current
        return render_current(job_id, clip_id, attempt.rq_task_id)
    if attempt.stage == 'transcribe':
        from app.core.transcribe import transcribe_video
        return transcribe_video(job_id)
    if attempt.stage == 'highlights':
        from app.core.highlights import detect_highlights
        return detect_highlights(job_id)
    if attempt.stage == 'cut':
        from app.core.clip_cutter import cut_clip
        return cut_clip(job_id, clip_id)
    if attempt.stage == 'reframe':
        from app.core.reframe import reframe_clip
        return reframe_clip(job_id, clip_id)
    if attempt.stage in ('captions', 'burn'):
        from app.core.captions import generate_captions, burn_captions
        return (generate_captions if attempt.stage == 'captions' else burn_captions)(job_id, clip_id)
    from app.core.scoring import score_and_title_clips
    return score_and_title_clips(job_id)


def _transient(exc):
    from app.core.llm_fallback import ProvidersUnavailable
    if isinstance(exc, ProvidersUnavailable):
        return exc.retryable
    if isinstance(exc, (requests.Timeout, requests.ConnectionError, TimeoutError)):
        return True
    return isinstance(exc, requests.HTTPError) and exc.response is not None and (
        exc.response.status_code == 429 or exc.response.status_code >= 500)


def _record_result(task_id, error=None, output_signature=None, elapsed=None):
    with Session(engine) as session:
        a = session.get(StageAttempt, task_id)
        run = session.get(PipelineRun, a.run_id)
        if run.current_task_id != task_id or not run.active_key:
            return
        a.status = 'failed' if error else 'completed'
        a.error_message = str(error) if error else None
        a.output_signature, a.elapsed_seconds, a.finished_at = output_signature, elapsed, now()
        if not error:
            a.input_signature = input_signature(a.job_id, a.stage, a.clip_id)
        session.add(a)
        failures = sum(x.unit_key == a.unit_key and x.run_id == a.run_id and x.status == 'failed'
                       for x in _attempts(session, a.job_id))
        retry = error and _transient(error) and failures < 3
        is_clip = a.clip_id is not None and not a.stage.endswith('_chunk') and a.stage != 'assess'
        if is_clip:
            key = f'{a.job_id}:{a.clip_id}'
            clip = session.get(ClipState, key) or ClipState(key=key, job_id=a.job_id, clip_id=a.clip_id, run_id=a.run_id)
            clip.run_id, clip.stage, clip.updated_at = a.run_id, a.stage, now()
            clip.status = 'retrying' if retry else 'failed' if error else 'completed' if a.stage in ('burn', 'render') else 'running'
            clip.error_message = str(error) if error else None
            session.add(clip)
        run.current_task_id, run.heartbeat_at = None, now()
        if error and not retry and not is_clip:
            _finish(session, run, 'failed', f'{a.stage}: {error}')
        else:
            run.status = 'retrying' if retry else 'running'
            job = session.get(Job, a.job_id)
            # Core compatibility functions may set status; active execution is
            # authoritative and terminal status is only set by _finish.
            completed_status = {'download': 'ingested', 'assets': 'ingested', 'transcribe_chunk': 'transcribed', 'highlights_chunk': 'highlights_ready', 'transcribe': 'transcribed', 'highlights': 'highlights_ready'}
            retry_status = {'assets': 'ingested', 'transcribe': 'ingested', 'highlights': 'transcribed'}
            job.status = (retry_status if error else completed_status).get(a.stage, 'clips_ready')
            job.error_message, job.updated_at = None, now()
            session.add(job)
            session.add(run)
        session.commit()


def run_stage(task_id):
    """RQ entry point; duplicates are ignored by an atomic attempt claim."""
    with Session(engine) as session:
        a = session.get(StageAttempt, task_id)
        if not a:
            return
        run = session.get(PipelineRun, a.run_id)
        if not run.active_key or run.current_task_id != task_id:
            return
        if run.cancel_requested:
            a.status, a.finished_at = 'cancelled', now()
            session.add(a)
            _finish(session, run, 'cancelled', 'Cancelled at a work-unit boundary')
            session.commit()
            return
        result = session.execute(update(StageAttempt).where(
            StageAttempt.attempt_id == task_id, StageAttempt.status == 'queued').values(
                status='running', started_at=now(), executions=StageAttempt.executions+1))
        if not result.rowcount:
            session.rollback()
            return
        run.status, run.heartbeat_at = 'running', now()
        session.add(run)
        session.commit()
        session.refresh(a)
        session.expunge(a)
    started = time.monotonic()
    try:
        with _heartbeat(a.run_id):
            _execute(a)
            sig = signature(a.job_id, a.stage, a.clip_id)
    except Exception as exc:
        _record_result(task_id, error=exc, elapsed=time.monotonic()-started)
        try:
            dispatch_next(a.run_id)
        except Exception:
            logger.exception('Could not dispatch following failed unit')
        raise  # RQ must record the same failure the DB reports.
    _record_result(task_id, output_signature=sig, elapsed=time.monotonic()-started)
    dispatch_next(a.run_id)


def run_pipeline(job_id):
    """Compatibility entrypoint for old queued jobs; now dispatches checkpoints."""
    run, _ = claim_run(job_id)
    dispatch_next(run.run_id)
