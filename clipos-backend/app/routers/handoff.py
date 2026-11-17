"""Optional search and Resolve handoff endpoints, scoped to one job."""
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session

from app.config import settings
from app.core.resolve_handoff import export_handoff
from app.models.job import Job, get_session

router = APIRouter(prefix='/jobs', tags=['handoff'])


class HandoffRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    clip_ids: list[str] | None = Field(default=None, max_length=100)


@router.get('/{job_id}/moments/search')
def search_moments(job_id: str, q: str = Query(min_length=1, max_length=200),
                   limit: int = Query(default=20, ge=1, le=100), session: Session = Depends(get_session)):
    if not session.get(Job, job_id):
        raise HTTPException(status_code=404, detail='Job not found')
    from app.core.selection import pool
    selected_path = Path(settings.storage_raw)/job_id/'highlights.json'
    selected = json.loads(selected_path.read_text()) if selected_path.is_file() else []
    chosen = {item['clip_id'] for item in selected}
    candidates = pool(job_id) or selected
    transcript_path = Path(settings.storage_raw)/job_id/'transcript.json'
    words = json.loads(transcript_path.read_text()) if transcript_path.is_file() else []
    query = q.casefold().strip()
    results = []
    for item in candidates:
        speech = ' '.join(word['word'] for word in words if word['end'] > item['start'] and word['start'] < item['end'])
        haystack = ' '.join((item.get('hook_text', ''), item.get('reasoning', ''), speech)).casefold()
        if query in haystack:
            results.append({'clip_id': item['clip_id'], 'start': item['start'], 'end': item['end'],
                            'hook_text': item.get('hook_text', ''), 'selected': item['clip_id'] in chosen,
                            'transcript_excerpt': speech[:500]})
        if len(results) >= limit:
            break
    return results


@router.post('/{job_id}/resolve-export')
def create_resolve_export(job_id: str, body: HandoffRequest, session: Session = Depends(get_session)):
    if not session.get(Job, job_id):
        raise HTTPException(status_code=404, detail='Job not found')
    try:
        return export_handoff(job_id, body.clip_ids, session.get_bind())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
