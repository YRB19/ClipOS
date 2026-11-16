from fastapi import APIRouter, Depends
from sqlmodel import Session
from app.models.job import get_session
from app.core.cache import cache_plan, clean_cache

router = APIRouter(prefix='/storage', tags=['storage'])


@router.get('/cache')
def inspect_cache(session: Session = Depends(get_session)):
    return cache_plan(session.get_bind())


@router.post('/cache/cleanup')
def cleanup(session: Session = Depends(get_session)):
    return clean_cache(session.get_bind())
