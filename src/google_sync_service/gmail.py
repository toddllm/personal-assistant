from __future__ import annotations

import base64
from collections import Counter
from datetime import UTC, datetime, timedelta
import html as html_lib
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
        max_results = int(request.max_results or self._settings.default_sync_max_results)
        max_results = max(1, min(max_results, self._settings.sync_max_results_cap))
        label_ids = [value for value in (request.label_ids or self._settings.default_label_ids_list) if value]

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
            _raise_for_status_with_detail(list_response, context="Gmail list")
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
    params = {"format": "full"}
    response = http.get(url, headers=headers, params=params)
    _raise_for_status_with_detail(response, context="Gmail message details")
    payload = response.json()

    payload_root = payload.get("payload") if isinstance(payload, dict) else {}
    if not isinstance(payload_root, dict):
        payload_root = {}

    header_map: dict[str, str] = {}
    for item in payload_root.get("headers") or []:
        name = str(item.get("name") or "").strip().lower()
        value = str(item.get("value") or "").strip()
        if name:
            header_map[name] = value

    body_text = _extract_message_body_text(payload_root)
    if not body_text:
        body_text = str(payload.get("snippet") or "").strip() or None

    internal_date_ms = int(payload.get("internalDate") or 0)
    if internal_date_ms > 0:
        received_at = datetime.fromtimestamp(internal_date_ms / 1000.0, tz=UTC)
    else:
        received_at = datetime.now(UTC)

    return GmailMessage(
        message_id=str(payload.get("id") or message_id),
        thread_id=str(payload.get("threadId") or ""),
        snippet=str(payload.get("snippet") or ""),
        body_text=body_text,
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


def _extract_message_body_text(payload_root: dict[str, object]) -> str | None:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    for mime_type, encoded_data in _iter_body_parts(payload_root):
        decoded = _decode_base64url_text(encoded_data)
        if not decoded:
            continue
        lowered = mime_type.lower()
        if lowered.startswith("text/plain"):
            plain_parts.append(decoded)
        elif lowered.startswith("text/html"):
            html_parts.append(decoded)

    joined_plain = "\n\n".join(part.strip() for part in plain_parts if part.strip()).strip()
    if joined_plain:
        return _normalize_body_text(joined_plain)

    joined_html = "\n\n".join(part.strip() for part in html_parts if part.strip()).strip()
    if joined_html:
        return _normalize_body_text(_html_to_text(joined_html))

    return None


def _iter_body_parts(node: object) -> list[tuple[str, str]]:
    if not isinstance(node, dict):
        return []

    out: list[tuple[str, str]] = []
    mime_type = str(node.get("mimeType") or "").strip()
    body = node.get("body")
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, str) and data.strip():
            out.append((mime_type, data.strip()))

    parts = node.get("parts")
    if isinstance(parts, list):
        for child in parts:
            out.extend(_iter_body_parts(child))
    return out


def _decode_base64url_text(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    padding = "=" * (-len(raw) % 4)
    try:
        decoded = base64.urlsafe_b64decode((raw + padding).encode("ascii"))
    except Exception:  # noqa: BLE001
        return ""
    return decoded.decode("utf-8", errors="replace")


def _html_to_text(html_value: str) -> str:
    content = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html_value)
    content = re.sub(r"(?i)<br\\s*/?>", "\n", content)
    content = re.sub(r"(?i)</p\\s*>", "\n\n", content)
    content = re.sub(r"(?i)</div\\s*>", "\n", content)
    content = re.sub(r"(?s)<[^>]+>", " ", content)
    content = html_lib.unescape(content)
    return content


def _normalize_body_text(value: str) -> str | None:
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return None
    max_chars = 16000
    if len(text) > max_chars:
        return f"{text[:max_chars]}..."
    return text


def _raise_for_status_with_detail(response: httpx.Response, *, context: str) -> None:
    if response.status_code < 400:
        return

    detail = _extract_error_detail(response)
    if detail:
        raise RuntimeError(f"{context} failed: {detail}")
    response.raise_for_status()


def _extract_error_detail(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        text = response.text.strip()
        return text or None

    if not isinstance(payload, dict):
        return str(payload)

    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if message:
            return str(message)
    detail = payload.get("detail")
    if detail:
        return str(detail)
    return json.dumps(payload, ensure_ascii=True)


__all__ = [
    "AuthError",
    "GmailSyncManager",
]
