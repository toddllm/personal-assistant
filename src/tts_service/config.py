from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TTS_SERVICE_", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8790
    log_level: str = "INFO"
    log_json: bool = False
    log_file_path: Path = Field(default_factory=lambda: Path("data/logs/tts-service.log"))
    log_file_max_bytes: int = 10 * 1024 * 1024
    log_file_backup_count: int = 7
    access_log: bool = True

    provider: str = "qwen3_custom_voice"
    startup_load_model: bool = True

    qwen_model_id: str = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
    qwen_device_map: str = "cuda:0"
    qwen_dtype: str = "bfloat16"
    qwen_attn_implementation: str | None = None
    qwen_import_path: str | None = None

    default_voice: str = "Vivian"
    default_language: str = "English"
    default_instruct: str | None = None

    max_text_chars: int = 6000
    max_new_tokens: int = 4096
    temperature: float = 0.6

    output_dir: Path = Field(default_factory=lambda: Path("data/tts_outputs"))

    voice_profiles_dir: Path = Field(default_factory=lambda: Path("data/voice_profiles"))
    legacy_clones_json: str | None = None
    legacy_clones_audio_dir: str | None = None
    max_ref_audio_seconds: float = 30.0
    min_ref_audio_seconds: float = 3.0


settings = Settings()
