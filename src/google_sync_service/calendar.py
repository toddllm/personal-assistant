from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import logging
import os
from urllib.parse import quote

import httpx

from google_sync_service.auth import AuthError, GoogleAuthManager
from google_sync_service.config import Settings
from google_sync_service.schemas import CalendarEvent, CalendarSyncRequest, CalendarSyncResponse

logger = logging.getLogger(__name__)

CALENDAR_BASE_URL = "https://www.googleapis.com/calendar/v3/calendars"


class CalendarSyncManager:
    def __init__(self, settings: Settings, auth_manager: GoogleAuthManager):
        self._settings = settings
        self._auth_manager = auth_manager

    def sync(self, request: CalendarSyncRequest) -> CalendarSyncResponse:
        access_token = self._auth_manager.get_access_token()
        now = datetime.now(UTC)
        calendar_id = (request.calendar_id or self._settings.default_calendar_id).strip() or "primary"
        max_results = request.max_results or self._settings.default_calendar_sync_max_results
        max_results = max(1, min(int(max_results), self._settings.calendar_sync_max_results_cap))

        time_min = request.time_min or (now - timedelta(hours=max(1, self._settings.default_calendar_lookback_hours)))
        time_max = request.time_max or (now + timedelta(hours=max(1, self._settings.default_calendar_lookahead_hours)))
        if time_max <= time_min:
            raise ValueError("time_max must be greater than time_min")

        headers = {"Authorization": f"Bearer {access_token}"}
        params: dict[str, object] = {
            "maxResults": max_results,
            "singleEvents": bool(request.single_events),
            "orderBy": request.order_by or "startTime",
            "timeMin": _to_google_iso(time_min),
            "timeMax": _to_google_iso(time_max),
        }
        if request.query:
            params["q"] = request.query

        url = f"{CALENDAR_BASE_URL}/{quote(calendar_id, safe='')}/events"
        with httpx.Client(timeout=self._settings.oauth_timeout_seconds) as http:
            response = http.get(url, headers=headers, params=params)
            if response.status_code >= 400:
                detail = _google_error_detail(response)
                raise RuntimeError(detail)
            payload = response.json()
            events = [
                _parse_calendar_event(raw=raw, calendar_id=calendar_id)
                for raw in (payload.get("items") or [])
            ]
            events = [event for event in events if event is not None]

        result = CalendarSyncResponse(
            synced_at=now,
            calendar_id=calendar_id,
            count=len(events),
            events=events,
        )
        self._write_cache(result)
        return result

    def load_cache(self) -> CalendarSyncResponse | None:
        path = self._settings.calendar_cache_path
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return CalendarSyncResponse.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read calendar cache %s: %s", path, exc)
            return None

    def events(
        self,
        *,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
        calendar_id: str | None = None,
        max_results: int | None = None,
        query: str | None = None,
        use_cache: bool = True,
    ) -> list[CalendarEvent]:
        request = CalendarSyncRequest(
            calendar_id=calendar_id,
            time_min=time_min,
            time_max=time_max,
            max_results=max_results,
            query=query,
        )
        try:
            return self.sync(request).events
        except AuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not use_cache:
                raise
            cache = self.load_cache()
            if cache is None:
                raise RuntimeError(f"Calendar sync failed and no cache available: {exc}") from exc
            return _filter_events(cache.events, time_min=time_min, time_max=time_max, calendar_id=calendar_id)

    def _write_cache(self, payload: CalendarSyncResponse) -> None:
        path = self._settings.calendar_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            logger.debug("Could not chmod calendar cache %s: %s", path, exc)


def _to_google_iso(value: datetime) -> str:
    utc_value = value.astimezone(UTC)
    return utc_value.isoformat().replace("+00:00", "Z")


def _from_google_iso(value: str) -> datetime:
    cleaned = str(value).strip()
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_calendar_event(*, raw: object, calendar_id: str) -> CalendarEvent | None:
    if not isinstance(raw, dict):
        return None
    event_id = str(raw.get("id") or "").strip()
    if not event_id:
        return None

    start = raw.get("start") or {}
    end = raw.get("end") or {}
    if not isinstance(start, dict) or not isinstance(end, dict):
        return None

    all_day = False
    if start.get("dateTime"):
        start_at = _from_google_iso(str(start.get("dateTime")))
    elif start.get("date"):
        all_day = True
        start_at = _from_google_iso(f"{start.get('date')}T00:00:00+00:00")
    else:
        return None

    if end.get("dateTime"):
        end_at = _from_google_iso(str(end.get("dateTime")))
    elif end.get("date"):
        all_day = True
        end_at = _from_google_iso(f"{end.get('date')}T00:00:00+00:00")
    else:
        return None

    if end_at <= start_at:
        end_at = start_at + timedelta(minutes=1)

    organizer_email = None
    organizer = raw.get("organizer")
    if isinstance(organizer, dict):
        organizer_email = str(organizer.get("email") or "").strip() or None

    return CalendarEvent(
        event_id=event_id,
        calendar_id=calendar_id,
        status=str(raw.get("status") or "").strip() or None,
        summary=str(raw.get("summary") or "").strip() or None,
        description=str(raw.get("description") or "").strip() or None,
        location=str(raw.get("location") or "").strip() or None,
        html_link=str(raw.get("htmlLink") or "").strip() or None,
        hangout_link=str(raw.get("hangoutLink") or "").strip() or None,
        organizer_email=organizer_email,
        start_at=start_at,
        end_at=end_at,
        all_day=all_day,
    )


def _filter_events(
    events: list[CalendarEvent],
    *,
    time_min: datetime | None,
    time_max: datetime | None,
    calendar_id: str | None,
) -> list[CalendarEvent]:
    out: list[CalendarEvent] = []
    cal_filter = str(calendar_id or "").strip()
    for event in events:
        if cal_filter and event.calendar_id != cal_filter:
            continue
        if time_min and event.end_at < time_min.astimezone(UTC):
            continue
        if time_max and event.start_at > time_max.astimezone(UTC):
            continue
        out.append(event)
    out.sort(key=lambda item: (item.start_at, item.end_at))
    return out


def _google_error_detail(response: httpx.Response) -> str:
    status = f"Google Calendar API error {response.status_code}"
    try:
        payload = response.json()
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            message = str(err.get("message") or "").strip()
            reason = ""
            errors = err.get("errors")
            if isinstance(errors, list) and errors:
                first = errors[0]
                if isinstance(first, dict):
                    reason = str(first.get("reason") or "").strip()
            if message and reason:
                return f"{status}: {reason} - {message}"
            if message:
                return f"{status}: {message}"
    except Exception:  # noqa: BLE001
        pass
    text = (response.text or "").strip()
    if text:
        snippet = text.replace("\n", " ")
        return f"{status}: {snippet[:500]}"
    return status


__all__ = ["CalendarSyncManager"]
