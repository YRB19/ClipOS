"""Additive import options; the canonical jobs schema stays compatible."""
from sqlmodel import SQLModel, Field


class SourceImport(SQLModel, table=True):
    __tablename__ = 'source_imports'
    job_id: str = Field(primary_key=True)
    kind: str
    url: str | None = None
    status: str = 'ready'
    audio_stream_index: int | None = None
    error_message: str | None = None
