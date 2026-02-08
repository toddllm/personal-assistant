from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SourceStartRequest(BaseModel):
    source_type: Literal["mic", "ffmpeg"] = Field(description="Audio source implementation.")
    source_id: str | None = Field(default=None, description="Optional explicit source ID.")
    device: str | int | None = Field(default=None, description="Mic device name or index.")
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
    running: bool
    started_at: datetime | None = None
    details: dict[str, str] = Field(default_factory=dict)


class SourceAudioLevel(BaseModel):
    source_id: str
    level_dbfs: float
    peak_dbfs: float
    updated_at: datetime
    age_seconds: float
    silent: bool
    clipped: bool


class TranscriptItem(BaseModel):
    id: int
    source_id: str
    session_id: str | None = None
    started_at: datetime
    ended_at: datetime
    text: str
    speaker: str | None = None


class TranscriptPage(BaseModel):
    items: list[TranscriptItem]
    next_before_id: int | None = None
    has_more: bool = False


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
