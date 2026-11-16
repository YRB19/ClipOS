"""Immutable edit revisions and durable, revision-specific render outputs."""
from datetime import datetime
from sqlmodel import SQLModel, Field
from app.models.execution import now


class SourceAsset(SQLModel, table=True):
    __tablename__ = 'source_assets'
    job_id: str = Field(primary_key=True)
    fingerprint: str
    source_path: str
    source_size: int
    source_mtime_ns: int
    metadata_json: str
    artifacts_json: str = '{}'
    status: str = 'probed'
    error_message: str | None = None
    updated_at: datetime = Field(default_factory=now)


class ClipEdit(SQLModel, table=True):
    __tablename__ = 'clip_edits'
    key: str = Field(primary_key=True)
    job_id: str = Field(index=True)
    clip_id: str
    revision: int
    revision_id: str
    generated_signature: str | None = None
    updated_at: datetime = Field(default_factory=now)


class EditRevision(SQLModel, table=True):
    __tablename__ = 'edit_revisions'
    revision_id: str = Field(primary_key=True)
    edit_key: str = Field(index=True)
    revision: int
    recipe_json: str
    created_at: datetime = Field(default_factory=now)


class RenderOutput(SQLModel, table=True):
    __tablename__ = 'render_outputs'
    render_id: str = Field(primary_key=True)
    job_id: str = Field(index=True)
    clip_id: str
    revision_id: str = Field(index=True)
    kind: str  # preview | final
    renderer_version: str
    active_key: str | None = Field(default=None, unique=True)
    rq_task_id: str
    status: str = 'queued'
    output_path: str | None = None
    output_signature: str | None = None
    metadata_json: str | None = None
    error_message: str | None = None
    created_at: datetime = Field(default_factory=now)
    started_at: datetime | None = None
    heartbeat_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None
