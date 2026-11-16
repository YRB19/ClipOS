"""Durable RQ renders pinned to immutable recipe revisions."""
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
from app.config import settings
from app.core.assets import ensure_source, artifact_stamp, source_unchanged
from app.core.recipes import EditRecipe, ensure_edit, validate_source
from app.core.renderer import RENDERER_VERSION, render_recipe
from app.models.job import engine
from app.models.edit import ClipEdit, EditRevision, RenderOutput
from app.models.execution import now


def output_available(output):
    try:
        return output.status == 'completed' and output.output_signature == artifact_stamp(output.output_path)
    except (OSError, TypeError):
        return False


def reserve_render(job_id, clip_id, revision_id, kind, task_id=None, db_engine=None):
    db_engine = db_engine or engine
    key = f'{job_id}:{clip_id}:{revision_id}:{kind}:{RENDERER_VERSION}'
    with Session(db_engine) as session:
        revision = session.get(EditRevision, revision_id)
        if not revision or revision.edit_key != f'{job_id}:{clip_id}':
            raise FileNotFoundError('Edit revision not found for this clip')
        if kind not in ('preview', 'final'):
            raise ValueError('Unknown render kind')
        existing = session.exec(select(RenderOutput).where(
            RenderOutput.job_id == job_id, RenderOutput.clip_id == clip_id,
            RenderOutput.revision_id == revision_id, RenderOutput.kind == kind,
            RenderOutput.renderer_version == RENDERER_VERSION).order_by(RenderOutput.created_at.desc())).first()
        if existing and (existing.active_key or output_available(existing)):
            session.expunge(existing)
            return existing, False
        render_id = str(uuid4())
        output = RenderOutput(render_id=render_id, job_id=job_id, clip_id=clip_id,
                              revision_id=revision_id, kind=kind, renderer_version=RENDERER_VERSION,
                              active_key=key, rq_task_id=task_id or render_id)
        session.add(output)
        try:
            session.commit()
            session.refresh(output)
            session.expunge(output)
            return output, True
        except IntegrityError:
            session.rollback()
            existing = session.exec(select(RenderOutput).where(RenderOutput.active_key == key)).one()
            session.expunge(existing)
            return existing, False


def request_render(job_id, clip_id, revision_id, kind, db_engine=None):
    db_engine = db_engine or engine
    output, created = reserve_render(job_id, clip_id, revision_id, kind, db_engine=db_engine)
    if created:
        try:
            from app.core.queue import get_queue
            get_queue().enqueue(run_render, output.render_id, job_id=output.rq_task_id,
                                job_timeout=settings.media_timeout_seconds+60, result_ttl=86400, failure_ttl=604800)
        except Exception as exc:
            with Session(db_engine) as session:
                current = session.get(RenderOutput, output.render_id)
                if current.status == 'queued':
                    current.status, current.active_key = 'failed', None
                    current.error_message, current.finished_at = f'Enqueue failed: {exc}', now()
                    session.add(current)
                    session.commit()
            raise
    return output.render_id


@contextmanager
def render_heartbeat(render_id):
    stopped = threading.Event()
    def pulse():
        while not stopped.wait(settings.heartbeat_seconds):
            with Session(engine) as session:
                session.execute(update(RenderOutput).where(RenderOutput.render_id == render_id,
                                RenderOutput.active_key.is_not(None)).values(heartbeat_at=now()))
                session.commit()
    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1)


def run_render(render_id):
    with Session(engine) as session:
        output = session.get(RenderOutput, render_id)
        if not output or not output.active_key:
            return  # Fences ambiguous delivery following failed enqueue/reconciliation.
        result = session.execute(update(RenderOutput).where(RenderOutput.render_id == render_id,
                                 RenderOutput.status == 'queued').values(status='running', started_at=now(), heartbeat_at=now()))
        if not result.rowcount:
            session.rollback()
            return
        session.commit()
        session.refresh(output)
        revision = session.get(EditRevision, output.revision_id)
        recipe_json = revision.recipe_json
        session.expunge(output)
    try:
        recipe = EditRecipe.model_validate_json(recipe_json)
        with render_heartbeat(render_id):
            asset = ensure_source(output.job_id)
            validate_source(recipe, asset, output.job_id)
            path = Path(settings.storage_clips)/output.job_id/output.clip_id/'renders'/render_id/f'{output.kind}.mp4'
            metadata = render_recipe(recipe, asset.source_path, json.loads(asset.metadata_json), output.kind, path)
            source_unchanged(asset)
        with Session(engine) as session:
            current = session.get(RenderOutput, render_id)
            if current.active_key is None or current.status != 'running':
                return  # A reconciled task never publishes its result.
            current.status, current.active_key = 'completed', None
            current.output_path, current.output_signature = str(path.resolve()), artifact_stamp(path)
            current.metadata_json, current.finished_at = json.dumps(metadata), now()
            session.add(current)
            session.commit()
    except Exception as exc:
        with Session(engine) as session:
            current = session.get(RenderOutput, render_id)
            current.status, current.active_key = 'failed', None
            current.error_message, current.finished_at = str(exc), now()
            session.add(current)
            session.commit()
        raise


def render_current(job_id, clip_id, task_id):
    edit = ensure_edit(job_id, clip_id)
    output, created = reserve_render(job_id, clip_id, edit['revision_id'], 'final', task_id=task_id)
    if created:
        run_render(output.render_id)
    with Session(engine) as session:
        current = session.get(RenderOutput, output.render_id)
        if not output_available(current):
            raise RuntimeError('Render not available; another task may still be rendering this revision')
        return current.output_path


def final_output(job_id, clip_id, db_engine=None):
    with Session(db_engine or engine) as session:
        head = session.get(ClipEdit, f'{job_id}:{clip_id}')
        if not head:
            return None
        outputs = session.exec(select(RenderOutput).where(RenderOutput.revision_id == head.revision_id,
                    RenderOutput.kind == 'final', RenderOutput.renderer_version == RENDERER_VERSION)
                    .order_by(RenderOutput.created_at.desc())).all()
        return next((o for o in outputs if output_available(o)), None)


def public_render(output, current_revision_id):
    available = output_available(output)
    url = None
    if available:
        # Paths exposed to clients are server-produced paths under clips storage.
        relative = Path(output.output_path).relative_to(Path(settings.storage_clips).resolve())
        url = '/files/clips/' + relative.as_posix()
    return dict(render_id=output.render_id, revision_id=output.revision_id, kind=output.kind,
                rq_task_id=output.rq_task_id, status=output.status, available=available, url=url,
                is_current=output.revision_id == current_revision_id,
                error_message=output.error_message, created_at=output.created_at,
                finished_at=output.finished_at, metadata=json.loads(output.metadata_json) if output.metadata_json else None)


def reconcile_renders(db_engine=None):
    from app.core.execution import rq_state, _age
    db_engine = db_engine or engine
    with Session(db_engine) as session:
        outputs = list(session.exec(select(RenderOutput).where(RenderOutput.active_key.is_not(None))).all())
        for output in outputs:
            try:
                state = rq_state(output.rq_task_id)
            except Exception:
                continue
            lost = state in ('failed', 'stopped', 'canceled')
            lost |= state in ('missing', 'finished') and _age(output.heartbeat_at) > settings.dispatch_grace_seconds
            lost |= bool(output.started_at and _age(output.started_at) > settings.media_timeout_seconds+120)
            if lost:
                session.execute(update(RenderOutput).where(RenderOutput.render_id == output.render_id,
                    RenderOutput.active_key.is_not(None), RenderOutput.heartbeat_at == output.heartbeat_at).values(
                        status='failed', active_key=None, finished_at=now(),
                        error_message=f'Render worker lost ({state}); request this revision again to resume'))
        session.commit()
