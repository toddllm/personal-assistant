from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AuthStartRequest(BaseModel):
    open_browser: bool = True


class AuthStartResponse(BaseModel):
    auth_url: str
    state: str
    expires_at: datetime
    opened_browser: bool = False


class AuthStatusResponse(BaseModel):
    configured: bool
    connected: bool
    needs_user_action: bool
    has_refresh_token: bool
    token_path: str
    scopes: list[str]
    last_error: str | None = None


class GmailSyncRequest(BaseModel):
    max_results: int | None = None
    query: str | None = None
    label_ids: list[str] | None = None


class GmailMessage(BaseModel):
    message_id: str
    thread_id: str
    snippet: str
    body_text: str | None = None
    from_header: str | None = None
    subject: str | None = None
    date_header: str | None = None
    label_ids: list[str] = Field(default_factory=list)
    received_at: datetime


class GmailSyncResponse(BaseModel):
    synced_at: datetime
    query: str | None = None
    label_ids: list[str] = Field(default_factory=list)
    count: int
    messages: list[GmailMessage] = Field(default_factory=list)


class SenderCount(BaseModel):
    sender: str
    count: int


class SignalsSummaryResponse(BaseModel):
    generated_at: datetime
    window_hours: int
    has_cache: bool
    message_count: int
    unread_count: int
    top_senders: list[SenderCount] = Field(default_factory=list)
    recent_subjects: list[str] = Field(default_factory=list)


class CalendarSyncRequest(BaseModel):
    calendar_id: str | None = None
    time_min: datetime | None = None
    time_max: datetime | None = None
    max_results: int | None = None
    query: str | None = None
    single_events: bool = True
    order_by: str | None = "startTime"


class CalendarEvent(BaseModel):
    event_id: str
    calendar_id: str
    status: str | None = None
    summary: str | None = None
    description: str | None = None
    location: str | None = None
    html_link: str | None = None
    hangout_link: str | None = None
    organizer_email: str | None = None
    start_at: datetime
    end_at: datetime
    all_day: bool = False


class CalendarSyncResponse(BaseModel):
    synced_at: datetime
    calendar_id: str
    count: int
    events: list[CalendarEvent] = Field(default_factory=list)
