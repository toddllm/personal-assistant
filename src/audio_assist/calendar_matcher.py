from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging

import httpx

from audio_assist.storage import SessionRecord

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CalendarEventSnapshot:
    event_id: str
    calendar_id: str
    title: str
    started_at: datetime
    ended_at: datetime
    html_link: str | None
    hangout_link: str | None


@dataclass(slots=True)
class CalendarSessionMatch:
    session_id: str
    event_id: str
    calendar_id: str
    title: str
    started_at: datetime
    ended_at: datetime
    html_link: str | None
    hangout_link: str | None
    match_score: float
    overlap_seconds: int
    distance_seconds: int


class CalendarMatcher:
    def __init__(
        self,
        *,
        enabled: bool,
        base_url: str,
        timeout_seconds: float,
        padding_minutes: int = 120,
        min_overlap_seconds: int = 180,
        max_gap_seconds: int = 1800,
    ):
        self._enabled = bool(enabled)
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = max(0.4, float(timeout_seconds))
        self._padding = timedelta(minutes=max(5, int(padding_minutes)))
        self._min_overlap_seconds = max(0, int(min_overlap_seconds))
        self._max_gap_seconds = max(0, int(max_gap_seconds))

    @property
    def enabled(self) -> bool:
        return self._enabled

    def match_sessions(self, sessions: list[SessionRecord]) -> dict[str, CalendarSessionMatch]:
        if not self._enabled or not sessions:
            return {}

        normalized = [session for session in sessions if session.session_id]
        if not normalized:
            return {}

        min_start = min(session.started_at for session in normalized) - self._padding
        max_end = max(session.ended_at for session in normalized) + self._padding
        events = self._fetch_events(min_start=min_start, max_end=max_end)
        if not events:
            return {}

        matches: dict[str, CalendarSessionMatch] = {}
        for session in normalized:
            best_match = self._match_one(session, events)
            if best_match is not None:
                matches[session.session_id] = best_match
        return matches

    def _fetch_events(self, *, min_start: datetime, max_end: datetime) -> list[CalendarEventSnapshot]:
        url = f"{self._base_url}/v1/calendar/events"
        params = {
            "time_min": min_start.astimezone(UTC).isoformat(),
            "time_max": max_end.astimezone(UTC).isoformat(),
            "max_results": 800,
            "use_cache": "true",
        }
        try:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Calendar event fetch failed: %s", exc)
            return []

        events: list[CalendarEventSnapshot] = []
        for raw in payload if isinstance(payload, list) else []:
            try:
                event_id = str(raw.get("event_id") or "").strip()
                if not event_id:
                    continue
                started_at = _coerce_dt(raw.get("start_at"))
                ended_at = _coerce_dt(raw.get("end_at"))
                if started_at is None or ended_at is None or ended_at <= started_at:
                    continue
                title = str(raw.get("summary") or "").strip() or "(Untitled meeting)"
                events.append(
                    CalendarEventSnapshot(
                        event_id=event_id,
                        calendar_id=str(raw.get("calendar_id") or "").strip() or "primary",
                        title=title,
                        started_at=started_at,
                        ended_at=ended_at,
                        html_link=str(raw.get("html_link") or "").strip() or None,
                        hangout_link=str(raw.get("hangout_link") or "").strip() or None,
                    )
                )
            except Exception:  # noqa: BLE001
                continue
        events.sort(key=lambda item: (item.started_at, item.ended_at))
        return events

    def _match_one(self, session: SessionRecord, events: list[CalendarEventSnapshot]) -> CalendarSessionMatch | None:
        session_start = session.started_at
        session_end = session.ended_at
        session_duration = max(1.0, (session_end - session_start).total_seconds())

        best_event: CalendarEventSnapshot | None = None
        best_overlap = 0
        best_distance = 10**9
        best_score = float("-inf")

        for event in events:
            overlap = int(
                max(
                    0.0,
                    (
                        min(session_end, event.ended_at) - max(session_start, event.started_at)
                    ).total_seconds(),
                )
            )
            if overlap > 0:
                distance = 0
            else:
                distance = int(
                    min(
                        abs((session_start - event.ended_at).total_seconds()),
                        abs((event.started_at - session_end).total_seconds()),
                    )
                )

            event_duration = max(1.0, (event.ended_at - event.started_at).total_seconds())
            duration_delta = abs(session_duration - event_duration)
            # High overlap dominates. Nearby events can still match when overlap is low.
            score = (overlap * 1.0) - (distance * 0.7) - (duration_delta * 0.05)

            if score > best_score:
                best_event = event
                best_overlap = overlap
                best_distance = distance
                best_score = score

        if best_event is None:
            return None
        if best_overlap < self._min_overlap_seconds and best_distance > self._max_gap_seconds:
            return None

        return CalendarSessionMatch(
            session_id=session.session_id,
            event_id=best_event.event_id,
            calendar_id=best_event.calendar_id,
            title=best_event.title,
            started_at=best_event.started_at,
            ended_at=best_event.ended_at,
            html_link=best_event.html_link,
            hangout_link=best_event.hangout_link,
            match_score=round(float(best_score), 2),
            overlap_seconds=int(best_overlap),
            distance_seconds=int(best_distance),
        )


def _coerce_dt(value: object) -> datetime | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


__all__ = ["CalendarMatcher", "CalendarSessionMatch"]

