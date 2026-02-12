"""Tests for the meeting detector module."""

from __future__ import annotations

from unittest.mock import patch

from audio_assist.meeting_detector import (
    MeetingDetector,
    MeetingInfo,
    _check_browser_meet,
    _check_teams,
    _check_zoom,
)


class TestCheckZoom:
    def test_returns_none_when_zoom_not_running(self):
        with patch(
            "audio_assist.meeting_detector._run_osascript",
            return_value="false",
        ):
            assert _check_zoom() is None

    def test_returns_meeting_info_when_in_meeting(self):
        # _check_zoom calls _run_osascript 3 times:
        # 1) process check -> "true"
        # 2) window names -> "Zoom Meeting"
        # 3) menu bar items -> "Mute Audio"
        with patch(
            "audio_assist.meeting_detector._run_osascript",
            side_effect=["true", "Zoom Meeting", "Mute Audio"],
        ):
            result = _check_zoom()
            assert result is not None
            assert result.active is True
            assert result.provider == "zoom"
            assert result.title == "Zoom Meeting"
            assert result.confidence == 0.8

    def test_filters_non_meeting_windows(self):
        # 1) process check -> "true"
        # 2) window names -> "Zoom Workplace" (non-meeting)
        with patch(
            "audio_assist.meeting_detector._run_osascript",
            side_effect=["true", "Zoom Workplace"],
        ):
            assert _check_zoom() is None


class TestCheckTeams:
    def test_returns_none_when_teams_not_running(self):
        with patch(
            "audio_assist.meeting_detector._run_osascript",
            return_value="false",
        ):
            assert _check_teams() is None

    def test_returns_meeting_info_for_call_window(self):
        def side_effect(script: str, timeout_seconds: float = 3.0) -> str:
            if "contains" in script:
                return "true"
            if "name of every window" in script:
                return "Call with John"
            return ""

        with patch(
            "audio_assist.meeting_detector._run_osascript",
            side_effect=side_effect,
        ):
            result = _check_teams()
            assert result is not None
            assert result.active is True
            assert result.provider == "teams"
            assert "Call with John" in result.title


class TestCheckBrowserMeet:
    def test_returns_none_when_no_browsers_running(self):
        with patch(
            "audio_assist.meeting_detector._run_osascript",
            return_value="",
        ):
            assert _check_browser_meet() is None

    def test_filters_non_meeting_tabs(self):
        # A browser tab with "Google Meet" landing page should be filtered
        with patch(
            "audio_assist.meeting_detector._run_osascript",
            return_value="Google Meet|||",
        ):
            assert _check_browser_meet() is None


class TestMeetingDetector:
    def test_initial_state_is_inactive(self):
        detector = MeetingDetector()
        assert detector.last_state.active is False
        assert detector.last_checked_at is None

    def test_detect_no_active_meeting(self):
        detector = MeetingDetector()
        with patch(
            "audio_assist.meeting_detector._run_osascript",
            return_value="",
        ):
            result = detector.detect()
            assert result.active is False
            assert detector.last_checked_at is not None

    def test_consecutive_active_count_increments(self):
        detector = MeetingDetector()
        meeting = MeetingInfo(active=True, provider="zoom", title="Test", confidence=0.8)
        with patch(
            "audio_assist.meeting_detector._check_zoom",
            return_value=meeting,
        ), patch(
            "audio_assist.meeting_detector._check_teams",
            return_value=None,
        ), patch(
            "audio_assist.meeting_detector._check_browser_meet",
            return_value=None,
        ), patch(
            "audio_assist.meeting_detector._check_webex",
            return_value=None,
        ):
            detector.detect()
            assert detector._consecutive_active == 1
            detector.detect()
            assert detector._consecutive_active == 2

    def test_consecutive_inactive_resets_meeting_started_at(self):
        detector = MeetingDetector()
        with patch(
            "audio_assist.meeting_detector._run_osascript",
            return_value="",
        ):
            detector.detect()
            detector.detect()
            assert detector._consecutive_inactive == 2
            assert detector._meeting_started_at is None

    def test_status_returns_expected_keys(self):
        detector = MeetingDetector()
        status = detector.status()
        assert "active" in status
        assert "provider" in status
        assert "consecutive_active" in status
        assert "consecutive_inactive" in status
