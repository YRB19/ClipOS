"""Optional markers/review feedback and auditable selection usage."""
import json
from pathlib import Path
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select
from app.config import settings
from app.core.assets import ensure_source
from app.core.selection import pool, feedback, decision
from app.models.job import Job, get_session
from app.models.selection import MomentMarker, SelectionFeedback, ModelCall
from app.models.execution import now
from app.routers.execution_guard import require_idle

router = APIRouter(prefix='/jobs', tags=['selection'])


class MarkerBody(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    time_seconds: float = Field(ge=0, strict=True)
    label: str = Field(default='', max_length=240)


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    decision: Literal['accepted', 'rejected', 'automatic']
    note: str = Field(default='', max_length=600)
    review_seconds: float | None = Field(default=None, ge=0, le=86400)


def job_required(job_id, session):
    job = session.get(Job, job_id)
    if not job: raise HTTPException(status_code=404, detail='Job not found')
    return job


@router.get('/{job_id}/markers')
def get_markers(job_id: str, session: Session = Depends(get_session)):
    job_required(job_id, session)
    return session.exec(select(MomentMarker).where(MomentMarker.job_id == job_id).order_by(MomentMarker.time_seconds)).all()


@router.post('/{job_id}/markers', status_code=201)
def add_marker(job_id: str, body: MarkerBody, session: Session = Depends(get_session)):
    job = job_required(job_id, session)
    require_idle(job_id, session)
    if body.time_seconds >= job.duration_seconds:
        raise HTTPException(status_code=422, detail='Marker must be within the recording')
    asset = ensure_source(job_id, session.get_bind())
    from sqlalchemy import text
    session.execute(text('UPDATE jobs SET updated_at=updated_at WHERE job_id=:id'), {'id': job_id})
    require_idle(job_id, session)
    marker = MomentMarker(job_id=job_id, source_fingerprint=asset.fingerprint, **body.model_dump())
    session.add(marker); session.commit(); session.refresh(marker)
    return marker


@router.delete('/{job_id}/markers/{marker_id}')
def remove_marker(job_id: str, marker_id: str, session: Session = Depends(get_session)):
    job_required(job_id, session)
    from sqlalchemy import text
    session.execute(text('UPDATE jobs SET updated_at=updated_at WHERE job_id=:id'), {'id': job_id})
    require_idle(job_id, session)
    marker = session.get(MomentMarker, marker_id)
    if not marker or marker.job_id != job_id: raise HTTPException(status_code=404, detail='Marker not found')
    session.delete(marker); session.commit()
    return {'removed': marker_id}


@router.get('/{job_id}/candidates')
def get_candidates(job_id: str, session: Session = Depends(get_session)):
    job_required(job_id, session)
    decisions = feedback(job_id, session.get_bind())
    selected_path = Path(settings.storage_raw)/job_id/'highlights.json'
    selected = json.loads(selected_path.read_text()) if selected_path.exists() else []
    chosen = {h['clip_id']: h for h in selected}
    candidates = pool(job_id) or selected  # Legacy/manual jobs have no candidate pool.
    return [dict(chosen.get(h['clip_id'], h), feedback=decision(h, decisions), selected=h['clip_id'] in chosen)
            for h in candidates]


@router.put('/{job_id}/clips/{clip_id}/feedback')
def save_feedback(job_id: str, clip_id: str, body: FeedbackBody, session: Session = Depends(get_session)):
    job_required(job_id, session)
    # A short SQLite write lock serializes this check with claim_run. Manual
    # feedback is optional and applies on resume; never mutate running ranking.
    from sqlalchemy import text
    session.execute(text('UPDATE jobs SET updated_at=updated_at WHERE job_id=:id'), {'id': job_id})
    require_idle(job_id, session)
    candidates = pool(job_id)
    path = Path(settings.storage_raw)/job_id/'highlights.json'
    if path.exists(): candidates += json.loads(path.read_text())
    if clip_id not in {h['clip_id'] for h in candidates}:
        raise HTTPException(status_code=404, detail='Candidate not found')
    key = f'{job_id}:{clip_id}'
    item = session.get(SelectionFeedback, key) or SelectionFeedback(key=key, job_id=job_id, clip_id=clip_id, decision='automatic')
    for name, value in body.model_dump().items(): setattr(item, name, value)
    item.updated_at = now(); session.add(item); session.commit(); session.refresh(item)
    return item


@router.get('/{job_id}/selection-metrics')
def metrics(job_id: str, session: Session = Depends(get_session)):
    job_required(job_id, session)
    calls = session.exec(select(ModelCall).where(ModelCall.job_id == job_id).order_by(ModelCall.created_at)).all()
    reviews = session.exec(select(SelectionFeedback).where(SelectionFeedback.job_id == job_id)).all()
    return dict(calls=[c.model_dump() for c in calls], totals=dict(calls=len(calls),
        latency_seconds=sum(c.latency_seconds for c in calls),
        known_cost_credits=sum(c.cost_credits for c in calls if c.cost_credits is not None),
        calls_without_cost=sum(c.cost_credits is None for c in calls),
        known_prompt_tokens=sum(c.prompt_tokens or 0 for c in calls),
        known_completion_tokens=sum(c.completion_tokens or 0 for c in calls)),
        feedback=dict(accepted=sum(f.decision == 'accepted' for f in reviews),
                      rejected=sum(f.decision == 'rejected' for f in reviews),
                      review_seconds=sum(f.review_seconds or 0 for f in reviews) if any(f.review_seconds is not None for f in reviews) else None,
                      reviews_without_timing=sum(f.review_seconds is None for f in reviews)))
