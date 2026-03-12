from datetime import datetime

from pydantic import BaseModel, Field


class DiarizeChunkRequest(BaseModel):
    source_id: str
    session_id: str | None = None
    sample_rate: int = Field(ge=8_000, le=192_000)
    channels: int = Field(default=1, ge=1, le=8)
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


class SpeakerProfileResponse(BaseModel):
    id: int
    name: str
    embedding_dim: int
    enrolled_at: str
    sample_count: int
    is_owner: bool


class EnrollRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    sample_rate: int = Field(ge=8_000, le=192_000)
    channels: int = Field(default=1, ge=1, le=8)
    pcm_s16le_base64: str
    is_owner: bool = False


class VerifyRequest(BaseModel):
    sample_rate: int = Field(ge=8_000, le=192_000)
    channels: int = Field(default=1, ge=1, le=8)
    pcm_s16le_base64: str
    profile_id: int | None = None


class VerifyMatch(BaseModel):
    profile_id: int
    name: str
    similarity: float = Field(ge=0.0, le=1.0)
    is_match: bool


class VerifyResponse(BaseModel):
    matches: list[VerifyMatch] = Field(default_factory=list)
    threshold: float = Field(ge=0.0, le=1.0)
