"""Tests for the session orchestrator state machine."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from audio_assist.meeting_detector import MeetingInfo
from audio_assist.orchestrator import (
    MeetingSession,
    OrchestratorState,
    PreflightResult,
    SessionOrchestrator,
)


def _make_detector(active: bool = False, provider: str = "zoom", title: str = "Test Meeting"):
    """Build a mock MeetingDetector."""
    detector = MagicMock()
    detector._consecutive_active = 3
    info = MeetingInfo(
        active=active,
        provider=provider,
        title=title,
        confidence=0.8,
        started_at=datetime.now(tz=UTC) if active else None,
    )
    detector.detect.return_value = info
    return detector


class TestOrchestratorInit:
    def test_starts_in_idle_state(self):
        detector = _make_detector()
        orch = SessionOrchestrator(detector)
        assert orch.state == OrchestratorState.IDLE
        assert orch.active_session is None
        assert orch.is_recording is False

    def test_clamps_poll_interval_minimum(self):
        detector = _make_detector()
        orch = SessionOrchestrator(detector, poll_interval_seconds=0.5)
        assert orch._poll_interval == 3.0

    def test_clamps_cooldown_minimum(self):
        detector = _make_detector()
        orch = SessionOrchestrator(detector, cooldown_seconds=1.0)
        assert orch._cooldown_seconds == 5.0


class TestIdleToPreparingTransition:
    def test_transitions_to_preparing_on_active_meeting(self):
        detector = _make_detector(active=True)
        orch = SessionOrchestrator(detector)
        orch._tick()
        assert orch.state == OrchestratorState.PREPARING

    def test_stays_idle_when_no_meeting(self):
        detector = _make_detector(active=False)
        orch = SessionOrchestrator(detector)
        orch._tick()
        assert orch.state == OrchestratorState.IDLE


class TestPreparingToRecordingTransition:
    def test_transitions_to_recording_after_preflight_passes(self):
        detector = _make_detector(active=True)
        orch = SessionOrchestrator(detector, min_active_polls_to_record=1)
        # First tick: idle -> preparing
        orch._tick()
        assert orch.state == OrchestratorState.PREPARING
        # Second tick: preparing -> recording (preflight passes by default)
        orch._tick()
        assert orch.state == OrchestratorState.RECORDING
        assert orch.active_session is not None
        assert orch.is_recording is True

    def test_returns_to_idle_if_meeting_disappears_during_prep(self):
        detector = _make_detector(active=True)
        orch = SessionOrchestrator(detector)
        orch._tick()
        assert orch.state == OrchestratorState.PREPARING
        # Meeting disappears
        detector.detect.return_value = MeetingInfo(active=False)
        orch._tick()
        assert orch.state == OrchestratorState.IDLE

    def test_custom_preflight_failure_delays_recording(self):
        detector = _make_detector(active=True)
        def fail_preflight():
            return PreflightResult(passed=False, reasons=["disk full"])
        orch = SessionOrchestrator(
            detector,
            min_active_polls_to_record=1,
            preflight_fn=fail_preflight,
            preparing_timeout_seconds=999.0,
        )
        orch._tick()  # idle -> preparing
        orch._tick()  # preflight fails, stay in preparing
        assert orch.state == OrchestratorState.PREPARING


class TestRecordingToCooldownTransition:
    def test_transitions_to_cooldown_when_meeting_ends(self):
        detector = _make_detector(active=True)
        orch = SessionOrchestrator(detector, min_active_polls_to_record=1)
        orch._tick()  # idle -> preparing
        orch._tick()  # preparing -> recording
        assert orch.state == OrchestratorState.RECORDING
        # Meeting ends
        detector.detect.return_value = MeetingInfo(active=False)
        orch._tick()
        assert orch.state == OrchestratorState.COOLDOWN

    def test_updates_title_during_recording(self):
        detector = _make_detector(active=True, title="Original Title")
        orch = SessionOrchestrator(detector, min_active_polls_to_record=1)
        orch._tick()  # idle -> preparing
        orch._tick()  # preparing -> recording
        # Title changes
        detector.detect.return_value = MeetingInfo(
            active=True, provider="zoom", title="Updated Title", confidence=0.9,
        )
        orch._tick()
        assert orch.active_session.title == "Updated Title"


class TestCooldownTransitions:
    def test_returns_to_idle_after_cooldown_expires(self):
        detector = _make_detector(active=True)
        orch = SessionOrchestrator(
            detector,
            min_active_polls_to_record=1,
            cooldown_seconds=5.0,
        )
        orch._tick()  # idle -> preparing
        orch._tick()  # preparing -> recording
        detector.detect.return_value = MeetingInfo(active=False)
        orch._tick()  # recording -> cooldown
        assert orch.state == OrchestratorState.COOLDOWN
        # Simulate cooldown expiry by backdating state_entered_at
        from datetime import timedelta

        orch._state_entered_at = datetime.now(tz=UTC) - timedelta(seconds=10)
        orch._tick()
        assert orch.state == OrchestratorState.IDLE
        assert orch.active_session is None

    def test_resumes_recording_if_meeting_returns_during_cooldown(self):
        detector = _make_detector(active=True)
        orch = SessionOrchestrator(detector, min_active_polls_to_record=1)
        orch._tick()  # idle -> preparing
        orch._tick()  # preparing -> recording
        detector.detect.return_value = MeetingInfo(active=False)
        orch._tick()  # recording -> cooldown
        assert orch.state == OrchestratorState.COOLDOWN
        # Meeting comes back
        detector.detect.return_value = MeetingInfo(
            active=True, provider="zoom", title="Test", confidence=0.8,
        )
        orch._tick()
        assert orch.state == OrchestratorState.RECORDING


class TestCallbacks:
    def test_on_session_start_called(self):
        detector = _make_detector(active=True)
        callback = MagicMock()
        orch = SessionOrchestrator(
            detector,
            min_active_polls_to_record=1,
            on_session_start=callback,
        )
        orch._tick()  # idle -> preparing
        orch._tick()  # preparing -> recording
        callback.assert_called_once()
        session = callback.call_args[0][0]
        assert isinstance(session, MeetingSession)
        assert session.provider == "zoom"

    def test_on_session_end_called(self):
        detector = _make_detector(active=True)
        callback = MagicMock()
        orch = SessionOrchestrator(
            detector,
            min_active_polls_to_record=1,
            cooldown_seconds=5.0,
            on_session_end=callback,
        )
        orch._tick()  # idle -> preparing
        orch._tick()  # preparing -> recording
        detector.detect.return_value = MeetingInfo(active=False)
        orch._tick()  # recording -> cooldown
        from datetime import timedelta

        orch._state_entered_at = datetime.now(tz=UTC) - timedelta(seconds=10)
        orch._tick()  # cooldown -> idle (ends session)
        callback.assert_called_once()
        session = callback.call_args[0][0]
        assert session.ended_at is not None

    def test_callback_error_does_not_crash_orchestrator(self):
        detector = _make_detector(active=True)
        callback = MagicMock(side_effect=RuntimeError("boom"))
        orch = SessionOrchestrator(
            detector,
            min_active_polls_to_record=1,
            on_session_start=callback,
        )
        orch._tick()  # idle -> preparing
        orch._tick()  # preparing -> recording (callback raises but shouldn't crash)
        assert orch.state == OrchestratorState.RECORDING


class TestStatus:
    def test_status_contains_expected_keys(self):
        detector = _make_detector()
        orch = SessionOrchestrator(detector)
        status = orch.status()
        assert status["state"] == "idle"
        assert status["active_session"] is None
        assert "stats" in status
        assert "config" in status
        assert status["stats"]["sessions_started"] == 0

    def test_stats_update_after_session(self):
        detector = _make_detector(active=True)
        orch = SessionOrchestrator(detector, min_active_polls_to_record=1)
        orch._tick()  # idle -> preparing
        orch._tick()  # preparing -> recording
        status = orch.status()
        assert status["stats"]["sessions_started"] == 1
        assert status["active_session"] is not None


class TestStopCleanup:
    def test_stop_ends_active_session(self):
        detector = _make_detector(active=True)
        callback = MagicMock()
        orch = SessionOrchestrator(
            detector,
            min_active_polls_to_record=1,
            on_session_end=callback,
        )
        orch._tick()  # idle -> preparing
        orch._tick()  # preparing -> recording
        assert orch.active_session is not None
        orch.stop()
        assert orch.state == OrchestratorState.IDLE
        assert orch.active_session is None
        callback.assert_called_once()
