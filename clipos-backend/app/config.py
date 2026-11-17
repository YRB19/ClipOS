from pydantic_settings import BaseSettings
from pydantic import Field, model_validator


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
    navigation_seconds: int = Field(default=60, ge=10, le=300)
    media_root: str | None = None
    chunk_seconds: int = Field(default=300, ge=30, le=900)
    chunk_overlap_seconds: int = Field(default=15, ge=0, le=30)
    asr_model: str = 'base'
    asr_vad: bool = True
    analysis_max_chars: int = Field(default=24000, ge=2000, le=60000)
    target_clip_count: int = Field(default=10, ge=1, le=30)
    clip_min_seconds: float = Field(default=15, ge=1, le=90)
    clip_max_seconds: float = Field(default=90, ge=15, le=300)
    candidate_total_seconds: int = Field(default=600, ge=15, le=2700)
    cache_max_gb: float = Field(default=20, gt=0)
    max_source_gb: float = Field(default=100, gt=0)
    download_timeout_seconds: int = Field(default=1800, ge=30, le=7200)
    text_models: list[str] = ['nvidia:z-ai/glm-5.2', 'nvidia:mistralai/mistral-nemotron', 'openrouter:z-ai/glm-5.2:free']
    vision_models: list[str] = []  # Explicit image-capable OpenRouter model IDs.
    llm_request_timeout_seconds: float = Field(default=90, ge=1, le=300)
    llm_deadline_seconds: float = Field(default=240, ge=1, le=900)
    llm_retries: int = Field(default=1, ge=0, le=2)
    llm_backoff_seconds: float = Field(default=1, ge=0, le=10)
    selection_audio_enabled: bool = False
    selection_scene_enabled: bool = False
    selection_vision_enabled: bool = False
    selection_shortlist_count: int = Field(default=20, ge=1, le=60)
    selection_candidates_per_chunk: int = Field(default=12, ge=1, le=30)
    selection_setup_seconds: float = Field(default=12, ge=0, le=60)
    selection_payoff_seconds: float = Field(default=18, ge=1, le=90)
    selection_audio_peak_ratio: float = Field(default=3, ge=1.1, le=20)
    selection_scene_threshold: float = Field(default=.25, ge=.01, le=1)
    selection_min_score: float = Field(default=.45, ge=0, le=1)
    vision_frames: int = Field(default=6, ge=3, le=10)

    @model_validator(mode='after')
    def clip_budgets(self):
        if self.clip_min_seconds > self.clip_max_seconds or self.clip_min_seconds > self.candidate_total_seconds:
            raise ValueError('Clip minimum must fit the maximum and total duration budget')
        for spec in self.text_models + self.vision_models:
            if ':' not in spec or spec.split(':', 1)[0] not in ('nvidia', 'openrouter') or not spec.split(':', 1)[1]:
                raise ValueError('Model configuration requires provider:model entries')
        if any(not spec.startswith('openrouter:') for spec in self.vision_models):
            raise ValueError('Vision currently supports OpenRouter image-capable models')
        if self.selection_vision_enabled and not self.vision_models:
            raise ValueError('Set explicit VISION_MODELS before enabling vision assessment')
        return self

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
