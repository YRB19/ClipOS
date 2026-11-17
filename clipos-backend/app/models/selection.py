"""Auditable selection signals, optional user feedback and provider usage."""
from datetime import datetime
from uuid import uuid4
from sqlmodel import SQLModel, Field
from app.models.execution import now


class MomentMarker(SQLModel, table=True):
    __tablename__ = 'moment_markers'
    marker_id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    job_id: str = Field(index=True)
    source_fingerprint: str
    time_seconds: float
    label: str = ''
    created_at: datetime = Field(default_factory=now)


class SelectionFeedback(SQLModel, table=True):
    __tablename__ = 'selection_feedback'
    key: str = Field(primary_key=True)
    job_id: str = Field(index=True)
    clip_id: str
    decision: str  # accepted | rejected | automatic
    note: str = ''
    review_seconds: float | None = None
    updated_at: datetime = Field(default_factory=now)


class ModelCall(SQLModel, table=True):
    __tablename__ = 'model_calls'
    call_id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    job_id: str | None = Field(default=None, index=True)
    stage: str
    model: str
    provider: str
    prompt_version: str
    attempt: int
    status: str
    latency_seconds: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_credits: float | None = None
    error_type: str | None = None
    created_at: datetime = Field(default_factory=now)
