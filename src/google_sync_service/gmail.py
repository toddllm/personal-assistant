from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
import json
import logging
import os
import re

import httpx

from google_sync_service.auth import AuthError, GoogleAuthManager
from google_sync_service.config import Settings
from google_sync_service.schemas import (
    GmailMessage,
    GmailSyncRequest,
    GmailSyncResponse,
    SenderCount,
    SignalsSummaryResponse,
)

logger = logging.getLogger(__name__)

GMAIL_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users"

EMAIL_RE = re.compile(r"<([^>]+)>")


class GmailSyncManager:
    def __init__(self, settings: Settings, auth_manager: GoogleAuthManager):
        self._settings = settings
        self._auth_manager = auth_manager

    def sync(self, request: GmailSyncRequest) -> GmailSyncResponse:
        access_token = self._auth_manager.get_access_token()
        max_results = request.max_results or self._settings.default_sync_max_results
        max_results = max(1, min(max_results, self._settings.sync_max_results_cap))
        label_ids = request.label_ids or self._settings.default_label_ids_list

        headers = {
            "Authorization": f"Bearer {access_token}",
        }

        list_params: dict[str, object] = {
            "maxResults": max_results,
        }
        if request.query:
            list_params["q"] = request.query
        if label_ids:
            list_params["labelIds"] = label_ids

        list_url = f"{GMAIL_BASE_URL}/{self._settings.user_id}/messages"
        with httpx.Client(timeout=self._settings.oauth_timeout_seconds) as http:
            list_response = http.get(list_url, headers=headers, params=list_params)
            list_response.raise_for_status()
            list_payload = list_response.json()
            entries = list_payload.get("messages") or []

            messages: list[GmailMessage] = []
            for entry in entries:
                message_id = str(entry.get("id") or "").strip()
                if not message_id:
                    continue
                try:
                    details = _fetch_message_details(http, headers, self._settings.user_id, message_id)
                    messages.append(details)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to fetch Gmail message %s: %s", message_id, exc)

        result = GmailSyncResponse(
            synced_at=datetime.now(UTC),
            query=request.query,
            label_ids=label_ids,
            count=len(messages),
            messages=messages,
        )
        self._write_cache(result)
        return result

    def load_cache(self) -> GmailSyncResponse | None:
        path = self._settings.cache_path
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return GmailSyncResponse.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read Gmail cache %s: %s", path, exc)
            return None

    def summary(self, *, window_hours: int = 24) -> SignalsSummaryResponse:
        cache = self.load_cache()
        now = datetime.now(UTC)
        if cache is None:
            return SignalsSummaryResponse(
                generated_at=now,
                window_hours=window_hours,
                has_cache=False,
                message_count=0,
                unread_count=0,
                top_senders=[],
                recent_subjects=[],
            )

        cutoff = now - timedelta(hours=max(1, window_hours))
        window_messages = [message for message in cache.messages if message.received_at >= cutoff]

        sender_counts: Counter[str] = Counter()
        unread_count = 0
        recent_subjects: list[str] = []

        for message in window_messages:
            sender = _canonical_sender(message.from_header)
            if sender:
                sender_counts[sender] += 1
            if "UNREAD" in message.label_ids:
                unread_count += 1
            subject = (message.subject or "").strip()
            if subject and subject not in recent_subjects:
                recent_subjects.append(subject)
            if len(recent_subjects) >= 12:
                break

        top_senders = [
            SenderCount(sender=sender, count=count)
            for sender, count in sender_counts.most_common(8)
        ]

        return SignalsSummaryResponse(
            generated_at=now,
            window_hours=max(1, window_hours),
            has_cache=True,
            message_count=len(window_messages),
            unread_count=unread_count,
            top_senders=top_senders,
            recent_subjects=recent_subjects,
        )

    def _write_cache(self, payload: GmailSyncResponse) -> None:
        path = self._settings.cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            logger.debug("Could not chmod Gmail cache %s: %s", path, exc)


def _fetch_message_details(
    http: httpx.Client,
    headers: dict[str, str],
    user_id: str,
    message_id: str,
) -> GmailMessage:
    url = f"{GMAIL_BASE_URL}/{user_id}/messages/{message_id}"
    params = {
        "format": "metadata",
        "metadataHeaders": ["From", "Subject", "Date"],
    }
    response = http.get(url, headers=headers, params=params)
    response.raise_for_status()
    payload = response.json()

    header_map: dict[str, str] = {}
    for item in (payload.get("payload") or {}).get("headers") or []:
        name = str(item.get("name") or "").strip().lower()
        value = str(item.get("value") or "").strip()
        if name:
            header_map[name] = value

    internal_date_ms = int(payload.get("internalDate") or 0)
    if internal_date_ms > 0:
        received_at = datetime.fromtimestamp(internal_date_ms / 1000.0, tz=UTC)
    else:
        received_at = datetime.now(UTC)

    return GmailMessage(
        message_id=str(payload.get("id") or message_id),
        thread_id=str(payload.get("threadId") or ""),
        snippet=str(payload.get("snippet") or ""),
        from_header=header_map.get("from"),
        subject=header_map.get("subject"),
        date_header=header_map.get("date"),
        label_ids=[str(label) for label in payload.get("labelIds") or []],
        received_at=received_at,
    )


def _canonical_sender(from_header: str | None) -> str | None:
    if not from_header:
        return None
    match = EMAIL_RE.search(from_header)
    if match:
        return match.group(1).strip().lower()
    return from_header.strip().lower()


__all__ = [
    "AuthError",
    "GmailSyncManager",
]
