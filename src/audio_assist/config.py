from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUDIO_ASSIST_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8787

    sample_rate: int = 16_000
    channels: int = 1
    segment_seconds: float = 4.5
    max_query_results: int = 8

    whisper_model: str = "base.en"
    whisper_compute_type: str = "int8"
    whisper_device: str = "cpu"
    whisper_language: str = "en"

    database_path: Path = Field(default_factory=lambda: Path("data/audio_assist.db"))
    archive_audio: bool = True
    archive_audio_dir: Path = Field(default_factory=lambda: Path("data/audio_segments"))

    qa_provider: str = "extractive"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.1:8b"
    ollama_timeout_seconds: float = 25.0

    tts_enabled: bool = True
    tts_service_url: str = "http://toddllm:8790"
    tts_timeout_seconds: float = 90.0
    tts_default_voice: str = "Vivian"
    tts_default_language: str = "English"
    tts_default_instruct: str | None = None

    mic_source_id: str = "desk-mic"
    mic_speaker_name: str = "Todd Deshane"

    speaker_enabled: bool = False
    speaker_service_url: str = "http://127.0.0.1:8791"
    speaker_timeout_seconds: float = 1.8
    speaker_cooldown_seconds: float = 30.0
    speaker_async_enrichment: bool = True
    speaker_queue_size: int = 1024
    speaker_min_confidence: float = 0.55
    speaker_backfill_enabled: bool = True
    speaker_backfill_interval_seconds: float = 20.0
    speaker_backfill_batch_size: int = 24
    speaker_backfill_since_seconds: int = 4 * 3600


settings = Settings()
