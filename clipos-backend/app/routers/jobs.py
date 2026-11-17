"""Job management endpoints: listing, detail, automatic pipeline, queue status."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select, col

from app.config import settings
from app.core.pipeline import claim_run, dispatch_next, _finish
from app.core.execution import latest_run, reconcile, snapshot
from app.models.execution import PipelineRun, now
from app.models.job import Job, get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _job_dict(job: Job, run=None) -> dict:
    from pathlib import Path as _Path
    source_filename = _Path(job.video_path).name if job.video_path else None
    return {
        "job_id": job.job_id,
        "run_status": run.status if run else None,
        "execution_stage": run.current_stage if run else None,
        "source_filename": source_filename,
        "status": job.status,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "duration_seconds": job.duration_seconds,
        "error_message": job.error_message,
    }


# ── GET /jobs ────────────────────────────────────────────────────────────────


@router.get("")
def list_jobs(
    status: str | None = Query(None, description="Filter by status"),
    session: Session = Depends(get_session),
):
    """List all jobs, most recent first.  Optional ?status= filter."""
    stmt = select(Job).order_by(col(Job.created_at).desc())
    result = session.exec(stmt)
    jobs = list(result.all())

    if status:
        jobs = [j for j in jobs if j.status == status]

    return [_job_dict(j, latest_run(session, j.job_id)) for j in jobs]


# ── GET /jobs/{job_id} ──────────────────────────────────────────────────────


@router.get("/{job_id}")
def get_job(job_id: str, session: Session = Depends(get_session)):
    """Single job detail with clip_ids if clips exist."""
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    result = _job_dict(job, latest_run(session, job_id))

    # Attach clip_ids if highlights.json exists (contains clip_ids after cut_clips)
    highlights_path = Path(settings.storage_raw) / job_id / "highlights.json"
    if highlights_path.exists():
        import json

        try:
            highlights = json.loads(highlights_path.read_text())
            result["clip_ids"] = [h["clip_id"] for h in highlights if "clip_id" in h]
        except Exception:
            result["clip_ids"] = []
    else:
        result["clip_ids"] = []

    return result


@router.put('/{job_id}/audio-track')
def select_audio_track(job_id: str, body: dict, session: Session = Depends(get_session)):
    from app.routers.execution_guard import require_idle
    from app.core.ffprobe import get_video_metadata
    from app.models.source_import import SourceImport
    require_idle(job_id, session)
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    track = body.get('audio_stream_index')
    metadata = get_video_metadata(job.video_path)
    if track is not None and (type(track) is not int or track not in [t['stream_index'] for t in metadata['audio_tracks']]):
        raise HTTPException(status_code=422, detail='Select an available audio stream index or null for automatic')
    request = session.get(SourceImport, job_id) or SourceImport(job_id=job_id, kind=job.source_type)
    request.audio_stream_index = track
    session.add(request)
    session.commit()
    return {'audio_stream_index': track}


# ── GET /jobs/{job_id}/highlights ────────────────────────────────────────────


@router.get("/{job_id}/highlights")
def get_highlights(job_id: str, session: Session = Depends(get_session)):
    """Return the full highlights data for a job."""
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    highlights_path = Path(settings.storage_raw) / job_id / "highlights.json"
    if not highlights_path.exists():
        raise HTTPException(status_code=404, detail=f"No highlights data for job {job_id}")

    import json
    try:
        data = json.loads(highlights_path.read_text())
        from app.models.execution import ClipState
        states = {c.clip_id: c for c in session.exec(select(ClipState).where(ClipState.job_id == job_id)).all()}
        from app.core.selection import feedback, decision
        decisions = feedback(job_id, session.get_bind())
        for h in data:
            h['feedback'] = decision(h, decisions)
            clip_dir = Path(settings.storage_clips) / job_id / h['clip_id']
            h['available_variants'] = [name for name in ('clip.mp4', 'clip_vertical.mp4', 'clip_captioned.mp4')
                                       if (clip_dir / name).is_file() and (clip_dir / name).stat().st_size > 0]
            from app.core.renders import final_output, public_render
            final = final_output(job_id, h['clip_id'], session.get_bind())
            h['final_render'] = public_render(final, final.revision_id) if final else None
            state = states.get(h['clip_id'])
            h['processing_status'] = state.status if state else None
            h['processing_error'] = state.error_message if state else None
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read highlights: {e}")


# ── POST /jobs/{job_id}/run ─────────────────────────────────────────────────


@router.post("/{job_id}/run")
def run_job(job_id: str, session: Session = Depends(get_session)):
    """Enqueue the full automatic pipeline for this job."""
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    db_engine = session.get_bind()
    previous = latest_run(session, job_id)
    if previous and previous.active_key:
        reconcile(previous.run_id, db_engine)
    from app.core.renders import reconcile_renders
    from app.models.edit import RenderOutput
    reconcile_renders(db_engine)
    pending_render = session.exec(select(RenderOutput).where(RenderOutput.job_id == job_id,
                        RenderOutput.active_key.is_not(None))).first()
    if pending_render and not (previous and previous.active_key):
        raise HTTPException(status_code=409, detail='A clip render is active; wait for it before starting the pipeline')
    run, created = claim_run(job_id, db_engine)
    try:
        dispatch_next(run.run_id, db_engine)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to enqueue pipeline; resume is available: {exc}")
    session.expire_all()
    run = session.get(PipelineRun, run.run_id)
    return {"run_id": run.run_id, "rq_job_id": run.current_task_id,
            "status": run.status, "created": created}


@router.post("/{job_id}/resume")
def resume_job(job_id: str, session: Session = Depends(get_session)):
    return run_job(job_id, session)


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str, session: Session = Depends(get_session)):
    if not session.get(Job, job_id):
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    run = latest_run(session, job_id)
    if run and run.active_key:
        run.cancel_requested = True
        run.status = 'cancelling'
        session.add(run)
        session.commit()
        # Queued work can be fenced immediately; running work finishes its unit.
        from app.models.execution import StageAttempt
        attempt = session.get(StageAttempt, run.current_task_id) if run.current_task_id else None
        from sqlalchemy import update
        cancelled = not attempt
        if attempt:
            result = session.execute(update(StageAttempt).where(
                StageAttempt.attempt_id == attempt.attempt_id,
                StageAttempt.status == 'queued').values(status='cancelled', finished_at=now()))
            cancelled = bool(result.rowcount)
        if cancelled:
            _finish(session, run, 'cancelled', 'Cancelled before the next work unit')
            session.commit()
        else:
            session.commit()
    return {"status": run.status if run else 'idle'}


@router.get("/{job_id}/queue_status")
def get_queue_status(job_id: str, session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    run = latest_run(session, job_id)
    if run and run.active_key:
        reconcile(run.run_id, session.get_bind())
        session.expire_all()
        job = session.get(Job, job_id)
    return snapshot(session, job)
