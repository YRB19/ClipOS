from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    nvidia_api_key: str = ""
    openrouter_api_key: str | None = None
    redis_url: str = "redis://localhost:6379"
    database_url: str = "sqlite:///./clipos.db"
    storage_raw: str = "./storage/raw"
    storage_clips: str = "./storage/clips"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
