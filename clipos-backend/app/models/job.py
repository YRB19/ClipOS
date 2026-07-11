from datetime import datetime, timezone

from sqlmodel import SQLModel, Field, Session, create_engine

from app.config import settings


class Job(SQLModel, table=True):
    __tablename__ = "jobs"

    job_id: str = Field(primary_key=True)
    source_type: str  # "upload" | "youtube"
    status: str  # "ingested" | "transcribed" | "highlights_ready" | "clips_ready" | "done" | "failed"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    video_path: str
    duration_seconds: float
    error_message: str | None = Field(default=None)


engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
)


def init_db(custom_engine=None) -> None:
    e = custom_engine or engine
    SQLModel.metadata.create_all(e)


def get_session(custom_engine=None):
    e = custom_engine or engine
    with Session(e) as session:
        yield session
