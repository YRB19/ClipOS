"""Additive execution records; canonical jobs/transcript/highlight schemas stay intact."""
from datetime import datetime, timezone
from sqlmodel import SQLModel, Field


def now():
    return datetime.now(timezone.utc)


class PipelineRun(SQLModel, table=True):
    __tablename__ = 'pipeline_runs'
    run_id: str = Field(primary_key=True)
    job_id: str = Field(index=True)
    # Nullable UNIQUE is the atomic single-active-run lock, including queued work.
    active_key: str | None = Field(default=None, unique=True)
    status: str = 'queued'
    current_stage: str = 'transcribe'
    current_task_id: str | None = None
    cancel_requested: bool = False
    created_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None
    heartbeat_at: datetime = Field(default_factory=now)
    error_message: str | None = None


class StageAttempt(SQLModel, table=True):
    __tablename__ = 'stage_attempts'
    attempt_id: str = Field(primary_key=True)
    run_id: str = Field(index=True)
    job_id: str = Field(index=True)
    unit_key: str
    stage: str
    clip_id: str | None = None
    rq_task_id: str = Field(index=True)
    status: str = 'queued'
    executions: int = 0
    created_at: datetime = Field(default_factory=now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    elapsed_seconds: float | None = None
    error_message: str | None = None
    output_signature: str | None = None
    input_signature: str | None = None


class ClipState(SQLModel, table=True):
    __tablename__ = 'clip_states'
    key: str = Field(primary_key=True)
    job_id: str = Field(index=True)
    clip_id: str
    run_id: str
    status: str = 'pending'
    stage: str = 'cut'
    error_message: str | None = None
    updated_at: datetime = Field(default_factory=now)
