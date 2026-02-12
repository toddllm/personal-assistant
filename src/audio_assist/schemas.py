from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SourceStartRequest(BaseModel):
    source_type: Literal["mic", "ffmpeg"] = Field(description="Audio source implementation.")
    source_id: str | None = Field(default=None, description="Optional explicit source ID.")
    language_hint: str | None = Field(
        default=None,
        description="Optional language hint for ASR (for example: es, en, fr). Use auto/empty for detection.",
    )
    device: str | int | None = Field(default=None, description="Mic device name or index.")
    channels: int | None = Field(
        default=None,
        description="Optional capture channel override for mic sources (for example 2 for stereo loopback).",
    )
    ffmpeg_input: str | None = Field(
        default=None,
        description="ffmpeg -i value (file path, URL, or device expression).",
    )
    ffmpeg_input_format: str | None = Field(
        default=None,
        description="Optional ffmpeg -f input format.",
    )
    ffmpeg_extra_args: list[str] = Field(
        default_factory=list,
        description="Optional extra ffmpeg args inserted before output flags.",
    )


class SourceStatus(BaseModel):
    source_id: str
    source_type: str
    source_role: str = "fallback"
    running: bool
    started_at: datetime | None = None
    details: dict[str, str] = Field(default_factory=dict)


class SourceLanguageHintRequest(BaseModel):
    source_id: str
    language_hint: str | None = Field(
        default=None,
        description="Language hint for ASR (for example: es, en). Use auto/empty to clear.",
    )


class SourceAudioLevel(BaseModel):
    source_id: str
    level_dbfs: float
    peak_dbfs: float
    updated_at: datetime
    age_seconds: float
    silent: bool
    clipped: bool


class TranscriptCalendarMatch(BaseModel):
    event_id: str
    calendar_id: str
    title: str
    started_at: datetime
    ended_at: datetime
    html_link: str | None = None
    hangout_link: str | None = None
    match_score: float
    overlap_seconds: int = 0
    distance_seconds: int = 0


class TranscriptItem(BaseModel):
    id: int
    source_id: str
    session_id: str | None = None
    started_at: datetime
    ended_at: datetime
    text: str
    speaker: str | None = None
    calendar_match: TranscriptCalendarMatch | None = None


class TranscriptPage(BaseModel):
    items: list[TranscriptItem]
    next_before_id: int | None = None
    has_more: bool = False


class EventTranscriptGroup(BaseModel):
    event_id: str
    calendar_id: str
    title: str
    started_at: datetime
    ended_at: datetime
    html_link: str | None = None
    hangout_link: str | None = None
    overlap_count: int = 0
    transcripts: list[TranscriptItem] = Field(default_factory=list)


class EventsPage(BaseModel):
    calendar_id: str | None = None
    synced_at: datetime | None = None
    count: int = 0
    events: list[EventTranscriptGroup] = Field(default_factory=list)


class TranscriptPostprocessRequest(BaseModel):
    source_id: str | None = None
    since_seconds: int | None = Field(default=3600, ge=1)
    limit: int = Field(default=30, ge=1, le=500)
    apply: bool = True
    language: str | None = None
    model_name: str | None = None
    include_unchanged: bool = False
    parallelism: int | None = Field(default=None, ge=1, le=8)
    beam_size: int | None = Field(default=None, ge=1, le=5)
    best_of: int | None = Field(default=None, ge=1, le=5)
    vad_filter: bool | None = None
    no_speech_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class TranscriptPostprocessItem(BaseModel):
    id: int
    source_id: str
    started_at: datetime
    ended_at: datetime
    old_text: str
    new_text: str | None = None
    changed: bool = False
    applied: bool = False
    detected_language: str | None = None
    language_probability: float | None = None
    error: str | None = None


class TranscriptPostprocessResult(BaseModel):
    scanned: int = 0
    changed: int = 0
    applied: int = 0
    missing_audio: int = 0
    failed: int = 0
    model_name: str
    language: str | None = None
    elapsed_ms: int = 0
    avg_ms_per_chunk: float | None = None
    chunks_per_second: float | None = None
    items: list[TranscriptPostprocessItem] = Field(default_factory=list)


class TranscriptTranslateRequest(BaseModel):
    transcript_ids: list[int] = Field(default_factory=list, max_length=120)
    source_language: str | None = None
    target_language: str = "English"
    model_name: str | None = None


class TranscriptTranslateItem(BaseModel):
    id: int
    translated_text: str | None = None
    cached: bool = False
    error: str | None = None


class TranscriptTranslateResult(BaseModel):
    model_name: str
    source_language: str | None = None
    target_language: str = "English"
    items: list[TranscriptTranslateItem] = Field(default_factory=list)


class TranscriptTitleRequest(BaseModel):
    transcript_ids: list[int] = Field(default_factory=list, max_length=600)
    model_name: str | None = None
    max_words: int = Field(default=8, ge=3, le=16)


class TranscriptTitleResult(BaseModel):
    title: str
    provider: str
    transcript_count: int = 0


class TranscriptIngestRequest(BaseModel):
    source_id: str
    session_id: str | None = None
    source_type: str | None = None
    started_at: datetime
    ended_at: datetime
    text: str
    speaker: str | None = None


class PCMIngestRequest(BaseModel):
    source_id: str
    session_id: str | None = None
    started_at: datetime
    ended_at: datetime
    pcm_base64: str
    sample_rate: int | None = None


class QueryRequest(BaseModel):
    question: str
    translate: bool = False
    source_id: str | None = None
    since_seconds: int | None = Field(default=3600, ge=1)
    limit: int | None = Field(default=None, ge=1, le=50)
    provider: Literal["extractive", "ollama"] | None = None
    ollama_model: str | None = None
    speak: bool = False
    tts_voice: str | None = None
    tts_language: str | None = None
    tts_instruct: str | None = None


class QueryAnswer(BaseModel):
    answer: str
    evidence: list[TranscriptItem]
    provider: str
    audio_base64: str | None = None
    audio_mime_type: str | None = None
    tts_provider: str | None = None
    tts_error: str | None = None


class TTSSynthesizeRequest(BaseModel):
    text: str = Field(min_length=1)
    tts_voice: str | None = None
    tts_language: str | None = None
    tts_instruct: str | None = None


class TranscriptExportRequest(BaseModel):
    source_id: str | None = None
    since_seconds: int = Field(default=3600, ge=1)
    limit: int = Field(default=5000, ge=1, le=50000)
    format: Literal["text", "json", "markdown"] = "text"
    include_speaker: bool = True
    include_timestamps: bool = True
    include_source: bool = False


class TranscriptExportResult(BaseModel):
    content: str
    format: str
    transcript_count: int = 0
    time_range_start: datetime | None = None
    time_range_end: datetime | None = None
    sources: list[str] = Field(default_factory=list)


class GoogleCalendarSyncRequest(BaseModel):
    calendar_id: str | None = None
    lookback_hours: int = Field(default=24, ge=1, le=168)
    lookahead_hours: int = Field(default=24, ge=1, le=168)
    max_results: int | None = Field(default=None, ge=1, le=1000)
    query: str | None = None
