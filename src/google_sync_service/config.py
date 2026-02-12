from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GOOGLE_SYNC_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8792
    log_level: str = "INFO"
    log_json: bool = False
    log_file_path: Path = Field(default_factory=lambda: Path("data/logs/google-sync-service.log"))
    log_file_max_bytes: int = 10 * 1024 * 1024
    log_file_backup_count: int = 7
    access_log: bool = True

    public_base_url: str = "http://127.0.0.1:8792"
    oauth_redirect_path: str = "/v1/auth/callback"

    client_id: str | None = None
    client_secret: str | None = None
    client_secret_path: Path | None = None

    oauth_scopes: str = (
        "https://www.googleapis.com/auth/gmail.readonly,"
        "https://www.googleapis.com/auth/calendar.readonly"
    )
    oauth_state_ttl_seconds: int = 900
    oauth_timeout_seconds: float = 20.0

    token_path: Path = Field(default_factory=lambda: Path("data/google_sync/tokens.json"))
    pending_auth_path: Path = Field(default_factory=lambda: Path("data/google_sync/pending_auth.json"))
    cache_path: Path = Field(default_factory=lambda: Path("data/google_sync/gmail_latest.json"))
    calendar_cache_path: Path = Field(default_factory=lambda: Path("data/google_sync/calendar_latest.json"))

    user_id: str = "me"
    default_sync_max_results: int = 25
    sync_max_results_cap: int = 100
    default_label_ids: str = "INBOX"
    default_calendar_id: str = "primary"
    default_calendar_sync_max_results: int = 250
    calendar_sync_max_results_cap: int = 1000
    default_calendar_lookback_hours: int = 24
    default_calendar_lookahead_hours: int = 24

    @property
    def redirect_uri(self) -> str:
        base = self.public_base_url.rstrip("/")
        path = self.oauth_redirect_path if self.oauth_redirect_path.startswith("/") else f"/{self.oauth_redirect_path}"
        return f"{base}{path}"

    @property
    def scopes_list(self) -> list[str]:
        values = [part.strip() for part in self.oauth_scopes.split(",")]
        return [value for value in values if value]

    @property
    def default_label_ids_list(self) -> list[str]:
        values = [part.strip() for part in self.default_label_ids.split(",")]
        return [value for value in values if value]


settings = Settings()
