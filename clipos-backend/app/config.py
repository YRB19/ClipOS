from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    nvidia_api_key: str = ""
    openrouter_api_key: str | None = None
    redis_url: str = "redis://localhost:6379"
    database_url: str = "sqlite:///./clipos.db"
    storage_raw: str = "./storage/raw"
    storage_clips: str = "./storage/clips"
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    ffmpeg_full_path: str | None = None  # legacy: executable or its bin directory
    media_timeout_seconds: int = Field(default=900, gt=0, le=7200)
    analysis_timeout_seconds: int = Field(default=1800, gt=0, le=7200)
    heartbeat_seconds: int = Field(default=10, gt=0, le=60)
    dispatch_grace_seconds: int = Field(default=60, ge=30, le=300)

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
