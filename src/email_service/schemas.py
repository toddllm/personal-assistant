from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    service: str
    google_sync_reachable: bool
    google_sync_configured: bool | None = None
    ollama_enabled: bool
    ollama_reachable: bool | None = None
    ollama_model: str | None = None
    cache_exists: bool
    cache_message_count: int
    now: datetime


class InboxRefreshRequest(BaseModel):
    force_sync: bool = True
    max_results: int | None = None
    query: str | None = None
    label_ids: list[str] | None = None
    allow_cache_fallback: bool = True


class SenderCount(BaseModel):
    sender: str
    count: int


class EmailMessage(BaseModel):
    message_id: str
    thread_id: str
    from_header: str | None = None
    sender_email: str | None = None
    subject: str | None = None
    snippet: str
    label_ids: list[str] = Field(default_factory=list)
    received_at: datetime
    unread: bool = False
    needs_reply: bool = False
    priority_score: int = 0


class EmailThread(BaseModel):
    thread_id: str
    subject: str | None = None
    latest_at: datetime
    message_count: int
    unread_count: int
    participants: list[str] = Field(default_factory=list)
    needs_reply: bool = False
    priority_score: int = 0
    message_ids: list[str] = Field(default_factory=list)


class InboxSnapshotResponse(BaseModel):
    generated_at: datetime
    source: str
    message_count: int
    unread_count: int
    thread_count: int
    focus_count: int
    top_senders: list[SenderCount] = Field(default_factory=list)
    messages: list[EmailMessage] = Field(default_factory=list)
    threads: list[EmailThread] = Field(default_factory=list)
    focus: list[EmailMessage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class AssistantStatusResponse(BaseModel):
    enabled: bool
    reachable: bool
    model: str
    url: str
    detail: str | None = None


class AssistantModelsResponse(BaseModel):
    reachable: bool
    models: list[str] = Field(default_factory=list)
    default_model: str
    detail: str | None = None


class AssistantQueryRequest(BaseModel):
    question: str
    refresh: bool = False
    focus_only: bool = False
    max_messages: int = 30
    model: str | None = None


class AssistantQueryResponse(BaseModel):
    answer: str
    provider: str = "ollama"
    model: str
    generated_at: datetime
    messages_used: int
    warnings: list[str] = Field(default_factory=list)
