"""Keep manual overrides from changing artifacts during automatic processing."""
from fastapi import Depends, HTTPException
from sqlmodel import Session, select
from app.models.job import get_session
from app.models.execution import PipelineRun


def require_idle(job_id: str, session: Session = Depends(get_session)):
    active = session.exec(select(PipelineRun).where(PipelineRun.active_key == job_id)).first()
    if active:
        raise HTTPException(status_code=409, detail='Automatic processing is active; cancel it before a manual override')
