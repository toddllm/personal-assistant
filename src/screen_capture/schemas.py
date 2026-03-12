"""Pydantic schemas for the screen capture service API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CaptureRequest(BaseModel):
    session_id: str
    quality: int | None = Field(default=None, ge=1, le=100)
    scale: float | None = Field(default=None, ge=0.1, le=1.0)


class CaptureRecord(BaseModel):
    id: int
    session_id: str
    captured_at: str
    file_path: str
    width: int
    height: int
    file_size_bytes: int
    ocr_text: str | None = None
    participants_detected: list[str] | None = None


class CaptureListResponse(BaseModel):
    captures: list[CaptureRecord]
    total: int


class DeleteResponse(BaseModel):
    deleted_count: int


class HealthResponse(BaseModel):
    status: str = "ok"
    uptime_seconds: float = 0.0


class ErrorResponse(BaseModel):
    detail: str
