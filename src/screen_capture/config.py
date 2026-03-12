"""Screen capture service settings."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCREEN_CAPTURE_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8794
    log_level: str = "INFO"
    log_json: bool = False
    log_file_path: Path = Field(
        default_factory=lambda: Path("data/logs/screen-capture.log")
    )
    log_file_max_bytes: int = 10 * 1024 * 1024
    log_file_backup_count: int = 5

    capture_interval_seconds: int = 15
    capture_quality: int = 50
    capture_scale: float = 0.5
    capture_format: str = "webp"
    capture_dir: Path = Field(default_factory=lambda: Path("data/screen_captures"))
    max_frames_per_session: int = 1000
    database_path: Path = Field(
        default_factory=lambda: Path("data/screen_capture.db")
    )
