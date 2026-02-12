from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUDIO_ASSIST_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8787
    log_level: str = "INFO"
    log_json: bool = False
    log_file_path: Path = Field(default_factory=lambda: Path("data/logs/audio-assist.log"))
    log_file_max_bytes: int = 10 * 1024 * 1024
    log_file_backup_count: int = 7
    access_log: bool = True

    sample_rate: int = 16_000
    channels: int = 1
    segment_seconds: float = 4.5
    segment_overlap_seconds: float = 0.6
    max_query_results: int = 8

    whisper_model: str = "base"
    whisper_compute_type: str = "int8"
    whisper_device: str = "cpu"
    whisper_language: str = "auto"
    whisper_beam_size: int = 1
    whisper_best_of: int = 1
    whisper_vad_filter: bool = True
    whisper_system_audio_vad_filter: bool = False
    whisper_no_speech_threshold: float = 1.0
    whisper_system_audio_no_speech_threshold: float = 1.0
    whisper_min_signal_dbfs: float = -70.0
    whisper_system_audio_min_signal_dbfs: float = -96.0
    whisper_fallback_on_empty: bool = True
    whisper_fallback_beam_size: int = 2
    whisper_fallback_best_of: int = 2
    whisper_fallback_vad_filter: bool = False
    whisper_fallback_no_speech_threshold: float = 1.0
    whisper_fallback_min_signal_dbfs: float = -70.0
    whisper_system_audio_fallback_min_signal_dbfs: float = -96.0
    transcription_queue_size: int = 1024
    transcription_drop_archive_fallback: bool = True
    whisper_postprocess_model: str = "large-v3"
    whisper_postprocess_compute_type: str = "int8"
    whisper_postprocess_device: str = "cpu"
    whisper_postprocess_language: str = "auto"
    whisper_postprocess_cpu_threads: int = 0
    whisper_postprocess_num_workers: int = 2
    whisper_postprocess_parallelism: int = 2
    whisper_postprocess_beam_size: int = 1
    whisper_postprocess_best_of: int = 1
    whisper_postprocess_vad_filter: bool = True
    whisper_postprocess_no_speech_threshold: float = 1.0

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

    capture_autostart_enabled: bool = True
    capture_autostart_mic_enabled: bool = True
    capture_autostart_mic_source_id: str = "desk-mic"
    capture_autostart_mic_device: str | int | None = None
    capture_autostart_retry_cooldown_seconds: float = 90.0
    capture_autostart_system_audio_enabled: bool = True
    capture_autostart_system_audio_source_id: str = "system-audio"
    capture_autostart_system_audio_device: str | int | None = None
    capture_autostart_system_audio_channels: int = 2
    capture_autostart_system_audio_ffmpeg_input: str | None = None
    capture_autostart_system_audio_ffmpeg_input_format: str | None = None
    capture_watchdog_enabled: bool = True
    capture_watchdog_interval_seconds: float = 20.0

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

    orchestrator_enabled: bool = True
    orchestrator_poll_interval_seconds: float = 8.0
    orchestrator_preparing_timeout_seconds: float = 15.0
    orchestrator_cooldown_seconds: float = 30.0
    orchestrator_min_active_polls: int = 2

    noise_suppression_enabled: bool = True
    noise_suppression_prop_decrease: float = 0.85
    noise_suppression_stationary: bool = True

    calendar_match_enabled: bool = False
    google_sync_service_url: str = "http://127.0.0.1:8792"
    calendar_match_timeout_seconds: float = 2.5
    calendar_match_padding_minutes: int = 120
    calendar_match_min_overlap_seconds: int = 180
    calendar_match_max_gap_seconds: int = 1800
    google_sync_autostart: bool = True
    google_sync_autostart_command: str = "google-sync-service"
    google_sync_autostart_timeout_seconds: float = 12.0
    google_sync_autostart_cwd: Path | None = None
    google_sync_client_secret_path: Path | None = None


settings = Settings()
