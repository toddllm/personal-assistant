from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SPEAKER_SERVICE_", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8791
    log_level: str = "INFO"
    log_json: bool = False
    log_file_path: Path = Field(default_factory=lambda: Path("data/logs/speaker-service.log"))
    log_file_max_bytes: int = 10 * 1024 * 1024
    log_file_backup_count: int = 7
    access_log: bool = True

    window_seconds: float = 0.9
    min_window_seconds: float = 0.45
    min_voice_dbfs: float = -46.0
    similarity_threshold: float = 0.88
    create_threshold: float = 0.74
    max_clusters_per_source: int = 10
    stale_source_seconds: int = 6 * 3600


settings = Settings()
