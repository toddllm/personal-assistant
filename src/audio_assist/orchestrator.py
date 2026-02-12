"""Session orchestrator: metadata-driven meeting capture state machine."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from audio_assist.meeting_detector import MeetingDetector, MeetingInfo

logger = logging.getLogger(__name__)


class OrchestratorState(str, Enum):
    IDLE = "idle"
    PREPARING = "preparing"
    RECORDING = "recording"
    COOLDOWN = "cooldown"


@dataclass(slots=True)
class MeetingSession:
    """Tracks a single meeting capture session."""

    session_id: str
    provider: str | None
    title: str | None
    started_at: datetime
    ended_at: datetime | None = None
    transcript_count: int = 0
    confidence: float = 0.0


@dataclass(slots=True)
class PreflightResult:
    """Result of capture preflight checks."""

    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


class SessionOrchestrator:
    """
    State machine for meeting-driven capture sessions.

    States:
        idle -> preparing -> recording -> cooldown -> idle

    The orchestrator polls the meeting detector and manages transitions
    based on meeting metadata, preflight checks, and guard timers.
    """

    def __init__(
        self,
        meeting_detector: MeetingDetector,
        *,
        poll_interval_seconds: float = 8.0,
        preparing_timeout_seconds: float = 15.0,
        cooldown_seconds: float = 30.0,
        min_active_polls_to_record: int = 2,
        on_session_start: "OnSessionStart | None" = None,
        on_session_end: "OnSessionEnd | None" = None,
        preflight_fn: "PreflightFn | None" = None,
    ):
        self._detector = meeting_detector
        self._poll_interval = max(3.0, float(poll_interval_seconds))
        self._preparing_timeout = max(5.0, float(preparing_timeout_seconds))
        self._cooldown_seconds = max(5.0, float(cooldown_seconds))
        self._min_active_polls = max(1, int(min_active_polls_to_record))

        self._on_session_start = on_session_start
        self._on_session_end = on_session_end
        self._preflight_fn = preflight_fn

        self._lock = Lock()
        self._state = OrchestratorState.IDLE
        self._state_entered_at = datetime.now(tz=UTC)
        self._active_session: MeetingSession | None = None
        self._recent_sessions: list[MeetingSession] = []
        self._max_recent_sessions = 20

        self._poll_count = 0
        self._sessions_started = 0
        self._sessions_completed = 0
        self._preflight_failures = 0

        self._thread: Thread | None = None
        self._stop_event = Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._run, daemon=True, name="orchestrator")
        self._thread.start()
        logger.info(
            "Session orchestrator started (poll_interval=%.1fs, cooldown=%.1fs).",
            self._poll_interval,
            self._cooldown_seconds,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        # End any active session
        with self._lock:
            if self._active_session is not None:
                self._end_session_locked()
            self._state = OrchestratorState.IDLE
            self._state_entered_at = datetime.now(tz=UTC)

    def _run(self) -> None:
        while not self._stop_event.wait(self._poll_interval):
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("Orchestrator tick failed.")

    def _tick(self) -> None:
        meeting = self._detector.detect()
        self._poll_count += 1

        with self._lock:
            if self._state == OrchestratorState.IDLE:
                self._handle_idle(meeting)
            elif self._state == OrchestratorState.PREPARING:
                self._handle_preparing(meeting)
            elif self._state == OrchestratorState.RECORDING:
                self._handle_recording(meeting)
            elif self._state == OrchestratorState.COOLDOWN:
                self._handle_cooldown(meeting)

    def _handle_idle(self, meeting: MeetingInfo) -> None:
        if meeting.active:
            self._transition_to(OrchestratorState.PREPARING)
            logger.info(
                "Meeting detected (%s: %s). Entering preparing state.",
                meeting.provider,
                meeting.title,
            )

    def _handle_preparing(self, meeting: MeetingInfo) -> None:
        now = datetime.now(tz=UTC)
        elapsed = (now - self._state_entered_at).total_seconds()

        if not meeting.active:
            # Meeting disappeared during prep - false positive
            self._transition_to(OrchestratorState.IDLE)
            logger.info("Meeting signal lost during preparing. Returning to idle.")
            return

        # Check if we've had enough consecutive active polls
        if self._detector._consecutive_active >= self._min_active_polls:
            # Run preflight checks
            preflight = self._run_preflight()
            if preflight.passed:
                self._start_session(meeting)
                self._transition_to(OrchestratorState.RECORDING)
                logger.info(
                    "Preflight passed. Recording meeting: %s (%s)",
                    meeting.title,
                    meeting.provider,
                )
            else:
                # Preflight failed but meeting is active - keep trying until timeout
                if elapsed > self._preparing_timeout:
                    # Force start anyway - don't miss the meeting
                    self._start_session(meeting)
                    self._transition_to(OrchestratorState.RECORDING)
                    logger.warning(
                        "Preflight failed but timeout reached. Force-starting recording: %s",
                        preflight.reasons,
                    )
        elif elapsed > self._preparing_timeout:
            # Timeout - just start recording
            self._start_session(meeting)
            self._transition_to(OrchestratorState.RECORDING)
            logger.info("Preparing timeout. Starting recording for: %s", meeting.title)

    def _handle_recording(self, meeting: MeetingInfo) -> None:
        if meeting.active:
            # Still in meeting - update session info
            if self._active_session is not None:
                if meeting.title and meeting.title != self._active_session.title:
                    self._active_session.title = meeting.title
                self._active_session.confidence = meeting.confidence
            return

        # Meeting ended - transition to cooldown
        logger.info(
            "Meeting ended. Entering cooldown for %.0fs.",
            self._cooldown_seconds,
        )
        self._transition_to(OrchestratorState.COOLDOWN)

    def _handle_cooldown(self, meeting: MeetingInfo) -> None:
        now = datetime.now(tz=UTC)
        elapsed = (now - self._state_entered_at).total_seconds()

        if meeting.active:
            # Meeting came back during cooldown (e.g. breakout room, rejoin)
            self._transition_to(OrchestratorState.RECORDING)
            logger.info("Meeting resumed during cooldown. Continuing recording.")
            return

        if elapsed >= self._cooldown_seconds:
            self._end_session_locked()
            self._transition_to(OrchestratorState.IDLE)
            logger.info("Cooldown complete. Session ended.")

    def _transition_to(self, state: OrchestratorState) -> None:
        prev = self._state
        self._state = state
        self._state_entered_at = datetime.now(tz=UTC)
        logger.debug("Orchestrator: %s -> %s", prev.value, state.value)

    def _run_preflight(self) -> PreflightResult:
        if self._preflight_fn is not None:
            try:
                return self._preflight_fn()
            except Exception:  # noqa: BLE001
                self._preflight_failures += 1
                logger.exception("Preflight function raised.")
                return PreflightResult(passed=False, reasons=["preflight_fn error"])
        # Default: always pass
        return PreflightResult(passed=True)

    def _start_session(self, meeting: MeetingInfo) -> None:
        now = datetime.now(tz=UTC)
        session_id = f"meeting-{now.strftime('%Y%m%dT%H%M%S')}"
        session = MeetingSession(
            session_id=session_id,
            provider=meeting.provider,
            title=meeting.title,
            started_at=now,
            confidence=meeting.confidence,
        )
        self._active_session = session
        self._sessions_started += 1

        if self._on_session_start is not None:
            try:
                self._on_session_start(session)
            except Exception:  # noqa: BLE001
                logger.exception("on_session_start callback failed.")

    def _end_session_locked(self) -> None:
        if self._active_session is None:
            return
        now = datetime.now(tz=UTC)
        self._active_session.ended_at = now
        self._sessions_completed += 1

        if self._on_session_end is not None:
            try:
                self._on_session_end(self._active_session)
            except Exception:  # noqa: BLE001
                logger.exception("on_session_end callback failed.")

        self._recent_sessions.append(self._active_session)
        if len(self._recent_sessions) > self._max_recent_sessions:
            self._recent_sessions = self._recent_sessions[-self._max_recent_sessions:]
        self._active_session = None

    def status(self) -> dict[str, object]:
        with self._lock:
            active_session = None
            if self._active_session is not None:
                active_session = {
                    "session_id": self._active_session.session_id,
                    "provider": self._active_session.provider,
                    "title": self._active_session.title,
                    "started_at": self._active_session.started_at,
                    "confidence": self._active_session.confidence,
                    "duration_seconds": (
                        (datetime.now(tz=UTC) - self._active_session.started_at).total_seconds()
                        if self._active_session.started_at
                        else 0
                    ),
                }

            recent = [
                {
                    "session_id": s.session_id,
                    "provider": s.provider,
                    "title": s.title,
                    "started_at": s.started_at,
                    "ended_at": s.ended_at,
                    "duration_seconds": (
                        (s.ended_at - s.started_at).total_seconds()
                        if s.ended_at and s.started_at
                        else None
                    ),
                }
                for s in reversed(self._recent_sessions[-5:])
            ]

            return {
                "state": self._state.value,
                "state_entered_at": self._state_entered_at,
                "active_session": active_session,
                "recent_sessions": recent,
                "stats": {
                    "poll_count": self._poll_count,
                    "sessions_started": self._sessions_started,
                    "sessions_completed": self._sessions_completed,
                    "preflight_failures": self._preflight_failures,
                },
                "config": {
                    "poll_interval_seconds": self._poll_interval,
                    "preparing_timeout_seconds": self._preparing_timeout,
                    "cooldown_seconds": self._cooldown_seconds,
                    "min_active_polls_to_record": self._min_active_polls,
                },
            }

    @property
    def state(self) -> OrchestratorState:
        with self._lock:
            return self._state

    @property
    def active_session(self) -> MeetingSession | None:
        with self._lock:
            return self._active_session

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._state == OrchestratorState.RECORDING


# Type aliases for callbacks
OnSessionStart = type(lambda session: None)
OnSessionEnd = type(lambda session: None)
PreflightFn = type(lambda: PreflightResult(passed=True))
