from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EMAIL_SERVICE_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8793

    log_level: str = "INFO"
    log_json: bool = False
    log_file_path: Path = Field(default_factory=lambda: Path("data/logs/email-service.log"))
    log_file_max_bytes: int = 10 * 1024 * 1024
    log_file_backup_count: int = 7
    access_log: bool = True

    google_sync_service_url: str = "http://127.0.0.1:8792"
    google_sync_timeout_seconds: float = 20.0
    google_sync_default_max_results: int = 50
    google_sync_default_label_ids: str = "INBOX"

    local_cache_path: Path = Field(default_factory=lambda: Path("data/email_service/inbox_latest.json"))
    focus_default_limit: int = 15
    urgent_terms: str = "urgent,asap,action required,deadline,follow up,follow-up"
    important_senders: str = ""

    @property
    def default_label_ids_list(self) -> list[str]:
        values = [part.strip() for part in self.google_sync_default_label_ids.split(",")]
        return [value for value in values if value]

    @property
    def urgent_terms_list(self) -> list[str]:
        values = [part.strip().lower() for part in self.urgent_terms.split(",")]
        return [value for value in values if value]

    @property
    def important_senders_list(self) -> list[str]:
        values = [part.strip().lower() for part in self.important_senders.split(",")]
        return [value for value in values if value]


settings = Settings()
