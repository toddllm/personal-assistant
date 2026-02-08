from datetime import datetime

from pydantic import BaseModel, Field


class DiarizeChunkRequest(BaseModel):
    source_id: str
    session_id: str | None = None
    sample_rate: int = Field(ge=8_000, le=192_000)
    started_at: datetime
    ended_at: datetime
    pcm_s16le_base64: str


class DiarizedSegment(BaseModel):
    speaker: str
    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)
    duration: float = Field(ge=0.0)
    confidence: float | None = None


class DiarizeChunkResponse(BaseModel):
    speaker: str | None = None
    confidence: float | None = None
    segments: list[DiarizedSegment] = Field(default_factory=list)

