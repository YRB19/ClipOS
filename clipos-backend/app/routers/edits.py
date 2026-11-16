"""Revision-safe edit, navigation-asset and queued render APIs."""
import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from sqlmodel import Session, select
from app.config import settings
from app.models.job import Job, get_session
from app.models.edit import SourceAsset, ClipEdit, RenderOutput
from app.core.recipes import EditRecipe, RevisionConflict, ensure_edit, save_edit
from app.core.renders import request_render, public_render, reconcile_renders
from app.core.assets import artifact_stamp, assets_valid
from app.routers.execution_guard import require_idle

router = APIRouter(prefix='/jobs', tags=['edits'])


class SaveEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0)
    recipe: EditRecipe


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision_id: str
    kind: Literal['preview', 'final'] = 'final'


def _job(session, job_id):
    if not session.get(Job, job_id):
        raise HTTPException(status_code=404, detail='Job not found')


def _translate(exc):
    if isinstance(exc, RevisionConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.get('/{job_id}/assets')
def get_assets(job_id: str, session: Session = Depends(get_session)):
    _job(session, job_id)
    asset = session.get(SourceAsset, job_id)
    if not asset:
        return dict(status='pending', artifacts={})
    artifacts = json.loads(asset.artifacts_json).get('files', {})
    urls = {}
    for name, item in artifacts.items():
        path = Path(item['path'])
        if path.is_file() and artifact_stamp(path) == item['signature']:
            urls[name] = '/files/raw/' + path.relative_to(Path(settings.storage_raw).resolve()).as_posix()
    return dict(status='stale' if asset.status == 'ready' and not assets_valid(asset) else asset.status, fingerprint=asset.fingerprint, metadata=json.loads(asset.metadata_json),
                artifacts=urls, error_message=asset.error_message)


@router.get('/{job_id}/clips/{clip_id}/edit')
def get_edit(job_id: str, clip_id: str, session: Session = Depends(get_session)):
    _job(session, job_id)
    try:
        return ensure_edit(job_id, clip_id, session.get_bind())
    except (ValueError, FileNotFoundError, RevisionConflict) as exc:
        raise _translate(exc)


@router.put('/{job_id}/clips/{clip_id}/edit', dependencies=[Depends(require_idle)])
def put_edit(job_id: str, clip_id: str, body: SaveEdit, session: Session = Depends(get_session)):
    _job(session, job_id)
    try:
        return save_edit(job_id, clip_id, body.recipe, body.expected_revision, session.get_bind())
    except (ValueError, FileNotFoundError, RevisionConflict) as exc:
        raise _translate(exc)


@router.post('/{job_id}/clips/{clip_id}/renders', status_code=202, dependencies=[Depends(require_idle)])
def post_render(job_id: str, clip_id: str, body: RenderRequest, session: Session = Depends(get_session)):
    _job(session, job_id)
    try:
        render_id = request_render(job_id, clip_id, body.revision_id, body.kind, session.get_bind())
    except (ValueError, FileNotFoundError) as exc:
        raise _translate(exc)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f'Render enqueue failed; request again to retry: {exc}')
    session.expire_all()
    head = session.get(ClipEdit, f'{job_id}:{clip_id}')
    return public_render(session.get(RenderOutput, render_id), head.revision_id if head else None)


@router.get('/{job_id}/clips/{clip_id}/renders')
def get_renders(job_id: str, clip_id: str, session: Session = Depends(get_session)):
    _job(session, job_id)
    reconcile_renders(session.get_bind())
    session.expire_all()
    head = session.get(ClipEdit, f'{job_id}:{clip_id}')
    outputs = session.exec(select(RenderOutput).where(RenderOutput.job_id == job_id,
                RenderOutput.clip_id == clip_id).order_by(RenderOutput.created_at.desc())).all()
    return [public_render(o, head.revision_id if head else None) for o in outputs]
