"""Read execution state and reconcile worker/dispatch loss without registry scans."""
import asyncio
import logging
from datetime import timezone

from redis import Redis
from rq.job import Job as RqJob
from rq.exceptions import NoSuchJobError
from sqlmodel import Session, select
from sqlalchemy import update

from app.config import settings
from app.models.job import engine
from app.models.execution import PipelineRun, StageAttempt, ClipState, now
from app.core.pipeline import _finish, dispatch_next, _plan, _valid, _attempts, _key
from app.core.queue import task_timeout

logger = logging.getLogger(__name__)


def latest_run(session, job_id):
    return session.exec(select(PipelineRun).where(PipelineRun.job_id == job_id)
                        .order_by(PipelineRun.created_at.desc())).first()


def _age(timestamp):
    return (now() - timestamp.replace(tzinfo=timezone.utc)).total_seconds()


def rq_state(task_id):
    connection = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    try:
        job = RqJob.fetch(task_id, connection=connection)
        status = job.get_status(refresh=True)
        return status.value if hasattr(status, 'value') else str(status)
    except NoSuchJobError:
        return 'missing'


def reconcile(run_id, db_engine=None):
    db_engine = db_engine or engine
    dispatch = False
    with Session(db_engine) as session:
        run = session.get(PipelineRun, run_id)
        if not run or not run.active_key:
            return
        if not run.current_task_id:
            dispatch = True
        else:
            attempt = session.get(StageAttempt, run.current_task_id)
            try:
                state = rq_state(run.current_task_id)
            except Exception:
                return  # Redis unavailable does not prove a worker died.
            lost = state in ('failed', 'stopped', 'canceled')
            lost |= state in ('missing', 'finished') and _age(run.heartbeat_at) > settings.dispatch_grace_seconds
            if attempt and attempt.status == 'running' and attempt.started_at:
                lost |= _age(attempt.started_at) > task_timeout(attempt.stage) + settings.dispatch_grace_seconds
            if lost:
                # A worker may have finished or heartbeated while Redis was read.
                # Fence reconciliation against exactly the observed task/lease.
                claimed = session.execute(update(PipelineRun).where(
                    PipelineRun.run_id == run_id,
                    PipelineRun.current_task_id == run.current_task_id,
                    PipelineRun.active_key.is_not(None),
                    PipelineRun.heartbeat_at == run.heartbeat_at).values(status='reconciling'))
                if not claimed.rowcount:
                    session.rollback()
                    return
                message = f'Worker/task lost ({state}); resume from verified checkpoints'
                if attempt:
                    attempt.status, attempt.error_message, attempt.finished_at = 'failed', message, now()
                    session.add(attempt)
                if attempt and attempt.stage == 'download':
                    from app.models.source_import import SourceImport
                    request = session.get(SourceImport, run.job_id)
                    if request:
                        request.status, request.error_message = 'failed', message
                        session.add(request)
                if attempt and attempt.stage == 'assets':
                    from app.models.edit import SourceAsset
                    asset = session.get(SourceAsset, run.job_id)
                    if asset:
                        asset.status, asset.error_message = 'failed', message
                        session.add(asset)
                _finish(session, run, 'cancelled' if run.cancel_requested else 'failed', message)
                session.commit()
    if dispatch:
        dispatch_next(run_id, db_engine)


def snapshot(session, job):
    run = latest_run(session, job.job_id)
    if not run:
        return dict(job_id=job.job_id, current_stage=job.status, rq_state='unknown',
                    error_message=job.error_message, started_at=None, finished_at=None,
                    run_status=None, stages=[], clips=[], completed_units=0, total_units=0)
    attempts = _attempts(session, job.job_id)
    plan = _plan(job.job_id, attempts, session.get_bind())
    latest = {}
    for a in attempts:
        latest.setdefault(a.unit_key, a)
    completed = sum(_valid(latest[_key(s, c)]) for s, c in plan if _key(s, c) in latest)
    stages = []
    for a in reversed(attempts):
        if a.run_id == run.run_id:
            stages.append(dict(stage=a.stage, clip_id=a.clip_id, status=a.status,
                               rq_task_id=a.rq_task_id, elapsed_seconds=a.elapsed_seconds,
                               error_message=a.error_message))
    candidate_ids = {clip_id for stage, clip_id in plan if clip_id and not stage.endswith('_chunk') and stage != 'assess'}
    clips = [c for c in session.exec(select(ClipState).where(ClipState.job_id == job.job_id)).all()
             if c.clip_id in candidate_ids]
    try:
        state = rq_state(run.current_task_id) if run.current_task_id else run.status
    except Exception:
        state = 'unknown'
    return dict(job_id=job.job_id, current_stage=job.status, execution_stage=run.current_stage,
                rq_state=state, run_id=run.run_id, run_status=run.status,
                rq_job_id=run.current_task_id, error_message=run.error_message or job.error_message,
                cancel_requested=run.cancel_requested, started_at=run.created_at,
                heartbeat_at=run.heartbeat_at, finished_at=run.finished_at,
                analyzed_chunks=sum(_valid(latest[_key(s, c)]) for s, c in plan
                                    if s == 'highlights_chunk' and _key(s, c) in latest),
                total_chunks=sum(s == 'highlights_chunk' for s, _ in plan),
                completed_units=completed, total_units=len(plan), stages=stages,
                completed_clips=sum(c.status == 'completed' for c in clips),
                failed_clips=sum(c.status == 'failed' for c in clips), clips=[c.model_dump() for c in clips])


def reconcile_active():
    from app.core.renders import reconcile_renders
    reconcile_renders(engine)
    with Session(engine) as session:
        ids = list(session.exec(select(PipelineRun.run_id).where(PipelineRun.active_key.is_not(None))).all())
    for run_id in ids:
        try:
            reconcile(run_id)
        except Exception:
            logger.exception('Reconciliation failed: %s', run_id)


async def reconciliation_loop():
    while True:
        await asyncio.to_thread(reconcile_active)
        await asyncio.sleep(15)
