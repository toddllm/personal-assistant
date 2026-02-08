from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SPEAKER_SERVICE_", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8791

    window_seconds: float = 0.9
    min_window_seconds: float = 0.45
    min_voice_dbfs: float = -46.0
    similarity_threshold: float = 0.88
    create_threshold: float = 0.74
    max_clusters_per_source: int = 10
    stale_source_seconds: int = 6 * 3600


settings = Settings()

