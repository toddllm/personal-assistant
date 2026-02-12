from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import re
from typing import Any

from fastapi import FastAPI, HTTPException, Query
import httpx

from email_service.config import Settings
from email_service.schemas import (
    EmailMessage,
    EmailThread,
    HealthResponse,
    InboxRefreshRequest,
    InboxSnapshotResponse,
    SenderCount,
)

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"<([^>]+)>")
REPLY_HINT_TERMS = (
    "please",
    "can you",
    "could you",
    "let me know",
    "need you",
    "action required",
    "follow up",
    "follow-up",
)


def summarize_gmail_messages(
    raw_messages: list[dict[str, Any]],
    *,
    settings: Settings,
    source: str,
    focus_limit: int | None = None,
    now: datetime | None = None,
) -> InboxSnapshotResponse:
    current = _utc_now() if now is None else _to_utc(now)
    focus_cap = max(1, int(focus_limit or settings.focus_default_limit))

    sender_counts: Counter[str] = Counter()
    messages: list[EmailMessage] = []
    thread_buckets: dict[str, dict[str, Any]] = {}

    for raw in raw_messages:
        message_id = _clean_string(raw.get("message_id") or raw.get("id"))
        if not message_id:
            continue

        thread_id = _clean_string(raw.get("thread_id") or raw.get("threadId")) or message_id
        from_header = _clean_string(raw.get("from_header") or raw.get("from"))
        sender_email = _canonical_sender(from_header)
        subject = _clean_string(raw.get("subject"))
        snippet = _clean_string(raw.get("snippet")) or ""
        received_at = _parse_datetime(raw.get("received_at"), fallback=current)

        raw_labels = raw.get("label_ids") or raw.get("labelIds") or []
        label_ids = [str(label).strip() for label in raw_labels if str(label).strip()]
        label_set = {label.upper() for label in label_ids}
        unread = "UNREAD" in label_set

        needs_reply = _needs_reply(unread=unread, subject=subject, snippet=snippet)
        priority_score = _priority_score(
            unread=unread,
            sender_email=sender_email,
            subject=subject,
            snippet=snippet,
            label_set=label_set,
            received_at=received_at,
            now=current,
            urgent_terms=settings.urgent_terms_list,
            important_senders=settings.important_senders_list,
            needs_reply=needs_reply,
        )

        message = EmailMessage(
            message_id=message_id,
            thread_id=thread_id,
            from_header=from_header,
            sender_email=sender_email,
            subject=subject,
            snippet=snippet,
            label_ids=label_ids,
            received_at=received_at,
            unread=unread,
            needs_reply=needs_reply,
            priority_score=priority_score,
        )
        messages.append(message)

        if sender_email:
            sender_counts[sender_email] += 1

        bucket = thread_buckets.get(thread_id)
        if bucket is None:
            bucket = {
                "thread_id": thread_id,
                "subject": subject,
                "latest_at": received_at,
                "message_count": 0,
                "unread_count": 0,
                "participants": set(),
                "needs_reply": False,
                "priority_score": 0,
                "message_ids": [],
            }
            thread_buckets[thread_id] = bucket

        bucket["message_count"] += 1
        if unread:
            bucket["unread_count"] += 1
        if sender_email:
            bucket["participants"].add(sender_email)
        if received_at > bucket["latest_at"]:
            bucket["latest_at"] = received_at
        if subject and not bucket["subject"]:
            bucket["subject"] = subject
        bucket["needs_reply"] = bool(bucket["needs_reply"] or needs_reply)
        bucket["priority_score"] = max(int(bucket["priority_score"]), priority_score)
        bucket["message_ids"].append(message_id)

    messages.sort(key=lambda item: (item.priority_score, item.received_at), reverse=True)

    threads: list[EmailThread] = []
    for bucket in thread_buckets.values():
        participants = sorted(bucket["participants"])
        threads.append(
            EmailThread(
                thread_id=bucket["thread_id"],
                subject=bucket["subject"],
                latest_at=bucket["latest_at"],
                message_count=bucket["message_count"],
                unread_count=bucket["unread_count"],
                participants=participants,
                needs_reply=bool(bucket["needs_reply"]),
                priority_score=int(bucket["priority_score"]),
                message_ids=bucket["message_ids"],
            )
        )
    threads.sort(key=lambda item: (item.priority_score, item.latest_at), reverse=True)

    focus = [
        message
        for message in messages
        if message.unread or message.needs_reply or message.priority_score >= 6
    ][:focus_cap]
    if not focus:
        focus = messages[:focus_cap]

    top_senders = [
        SenderCount(sender=sender, count=count)
        for sender, count in sender_counts.most_common(10)
    ]

    return InboxSnapshotResponse(
        generated_at=current,
        source=source,
        message_count=len(messages),
        unread_count=sum(1 for message in messages if message.unread),
        thread_count=len(threads),
        focus_count=len(focus),
        top_senders=top_senders,
        messages=messages,
        threads=threads,
        focus=focus,
        warnings=[],
    )


class EmailInboxManager:
    def __init__(self, settings: Settings):
        self._settings = settings

    def refresh(self, request: InboxRefreshRequest) -> InboxSnapshotResponse:
        try:
            source, payload = self._fetch_google_payload(request)
            raw_messages = payload.get("messages")
            if not isinstance(raw_messages, list):
                raise RuntimeError("google-sync-service returned an invalid payload.")

            snapshot = summarize_gmail_messages(
                raw_messages,
                settings=self._settings,
                source=source,
            )
            self._write_snapshot(snapshot)
            return snapshot
        except Exception as exc:  # noqa: BLE001
            logger.warning("email refresh failed: %s", exc)
            if not request.allow_cache_fallback:
                raise

            cached = self.load_snapshot()
            if cached is None:
                raise

            warnings = [*cached.warnings, f"refresh_failed: {exc}"]
            return cached.model_copy(update={"warnings": warnings, "source": "local_cache"})

    def load_snapshot(self) -> InboxSnapshotResponse | None:
        path = self._settings.local_cache_path
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return InboxSnapshotResponse.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to read email snapshot %s: %s", path, exc)
            return None

    def google_sync_status(self) -> tuple[bool, bool | None]:
        url = f"{self._settings.google_sync_service_url.rstrip('/')}/v1/auth/status"
        timeout = httpx.Timeout(self._settings.google_sync_timeout_seconds)
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(url)
            if response.status_code >= 400:
                return True, None
            payload = response.json()
            if not isinstance(payload, dict):
                return True, None
            configured = payload.get("configured")
            return True, bool(configured) if configured is not None else None
        except Exception:  # noqa: BLE001
            return False, None

    def _fetch_google_payload(self, request: InboxRefreshRequest) -> tuple[str, dict[str, Any]]:
        base_url = self._settings.google_sync_service_url.rstrip("/")
        timeout = httpx.Timeout(self._settings.google_sync_timeout_seconds)
        with httpx.Client(timeout=timeout) as client:
            if request.force_sync:
                sync_body: dict[str, Any] = {}
                max_results = request.max_results or self._settings.google_sync_default_max_results
                sync_body["max_results"] = max(1, int(max_results))
                sync_body["label_ids"] = request.label_ids or self._settings.default_label_ids_list
                if request.query:
                    sync_body["query"] = request.query
                response = client.post(f"{base_url}/v1/gmail/sync", json=sync_body)
                source = "google_sync_live"
            else:
                response = client.get(f"{base_url}/v1/gmail/latest")
                source = "google_sync_cache"

        if response.status_code >= 400:
            detail = _extract_error_detail(response)
            raise RuntimeError(f"google-sync-service {response.status_code}: {detail}")

        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("google-sync-service response is not a JSON object.")
        return source, payload

    def _write_snapshot(self, snapshot: InboxSnapshotResponse) -> None:
        path: Path = self._settings.local_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            logger.debug("could not chmod email snapshot %s: %s", path, exc)


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="email-service", version="0.1.0")
    manager = EmailInboxManager(settings)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        google_sync_reachable, google_sync_configured = manager.google_sync_status()
        snapshot = manager.load_snapshot()

        cache_exists = snapshot is not None
        cache_message_count = snapshot.message_count if snapshot else 0

        status = "ok"
        if not google_sync_reachable and not cache_exists:
            status = "degraded"

        return HealthResponse(
            status=status,
            service="email-service",
            google_sync_reachable=google_sync_reachable,
            google_sync_configured=google_sync_configured,
            cache_exists=cache_exists,
            cache_message_count=cache_message_count,
            now=_utc_now(),
        )

    @app.post("/v1/inbox/refresh", response_model=InboxSnapshotResponse)
    def inbox_refresh(request: InboxRefreshRequest) -> InboxSnapshotResponse:
        try:
            return manager.refresh(request)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/v1/inbox/overview", response_model=InboxSnapshotResponse)
    def inbox_overview(
        refresh: bool = Query(default=False),
        max_results: int | None = Query(default=None, ge=1, le=500),
        query: str | None = Query(default=None),
    ) -> InboxSnapshotResponse:
        if refresh:
            request = InboxRefreshRequest(
                force_sync=True,
                max_results=max_results,
                query=query,
                allow_cache_fallback=True,
            )
            try:
                return manager.refresh(request)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=str(exc)) from exc

        snapshot = manager.load_snapshot()
        if snapshot is None:
            raise HTTPException(
                status_code=404,
                detail="No local email snapshot yet. Call POST /v1/inbox/refresh first.",
            )
        return snapshot

    @app.get("/v1/inbox/focus", response_model=list[EmailMessage])
    def inbox_focus(limit: int = Query(default=15, ge=1, le=100)) -> list[EmailMessage]:
        snapshot = manager.load_snapshot()
        if snapshot is None:
            raise HTTPException(
                status_code=404,
                detail="No local email snapshot yet. Call POST /v1/inbox/refresh first.",
            )
        return snapshot.focus[:limit]

    @app.get("/v1/inbox/threads", response_model=list[EmailThread])
    def inbox_threads(limit: int = Query(default=50, ge=1, le=500)) -> list[EmailThread]:
        snapshot = manager.load_snapshot()
        if snapshot is None:
            raise HTTPException(
                status_code=404,
                detail="No local email snapshot yet. Call POST /v1/inbox/refresh first.",
            )
        return snapshot.threads[:limit]

    @app.get("/v1/inbox/messages", response_model=list[EmailMessage])
    def inbox_messages(limit: int = Query(default=100, ge=1, le=1000)) -> list[EmailMessage]:
        snapshot = manager.load_snapshot()
        if snapshot is None:
            raise HTTPException(
                status_code=404,
                detail="No local email snapshot yet. Call POST /v1/inbox/refresh first.",
            )
        return snapshot.messages[:limit]

    return app


def _clean_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_datetime(value: object, *, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return _to_utc(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
            return _to_utc(parsed)
        except ValueError:
            return fallback
    return fallback


def _canonical_sender(from_header: str | None) -> str | None:
    if not from_header:
        return None
    match = EMAIL_RE.search(from_header)
    if match:
        return match.group(1).strip().lower() or None
    return from_header.strip().lower() or None


def _needs_reply(*, unread: bool, subject: str | None, snippet: str) -> bool:
    if not unread:
        return False
    combined = " ".join(part for part in [subject or "", snippet] if part).lower()
    if "?" in combined:
        return True
    return any(term in combined for term in REPLY_HINT_TERMS)


def _priority_score(
    *,
    unread: bool,
    sender_email: str | None,
    subject: str | None,
    snippet: str,
    label_set: set[str],
    received_at: datetime,
    now: datetime,
    urgent_terms: list[str],
    important_senders: list[str],
    needs_reply: bool,
) -> int:
    score = 0

    if unread:
        score += 4
    if needs_reply:
        score += 2
    if sender_email and sender_email in important_senders:
        score += 4

    text = " ".join(part for part in [subject or "", snippet] if part).lower()
    if any(term in text for term in urgent_terms):
        score += 3

    if "IMPORTANT" in label_set:
        score += 2

    age_seconds = max(0.0, (now - received_at).total_seconds())
    if age_seconds <= 6 * 3600:
        score += 3
    elif age_seconds <= 24 * 3600:
        score += 2
    elif age_seconds <= 72 * 3600:
        score += 1

    return score


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        text = response.text.strip()
        return text or f"HTTP {response.status_code}"

    if isinstance(payload, dict):
        detail = payload.get("detail")
        if detail:
            return str(detail)
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return str(message)
        return json.dumps(payload, ensure_ascii=True)

    return str(payload)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = ["EmailInboxManager", "create_app", "summarize_gmail_messages"]
