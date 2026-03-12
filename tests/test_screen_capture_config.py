"""Tests for screen capture config fields."""

from pathlib import Path

import pytest

from audio_assist.config import Settings


class TestScreenCaptureConfig:
    """Verify screen capture config defaults and env overrides."""

    def test_screen_capture_disabled_by_default(self):
        s = Settings()
        assert s.screen_capture_enabled is False

    def test_interval_seconds_default(self):
        s = Settings()
        assert s.screen_capture_interval_seconds == 15

    def test_quality_default(self):
        s = Settings()
        assert s.screen_capture_quality == 50

    def test_scale_default(self):
        s = Settings()
        assert s.screen_capture_scale == pytest.approx(0.5)

    def test_format_default(self):
        s = Settings()
        assert s.screen_capture_format == "webp"

    def test_capture_dir_default(self):
        s = Settings()
        assert s.screen_capture_dir == Path("data/screen_captures")

    def test_service_url_default(self):
        s = Settings()
        assert s.screen_capture_service_url == "http://127.0.0.1:8794"

    def test_max_frames_default(self):
        s = Settings()
        assert s.screen_capture_max_frames_per_session == 1000

    def test_env_override_interval(self, monkeypatch):
        monkeypatch.setenv("AUDIO_ASSIST_SCREEN_CAPTURE_INTERVAL_SECONDS", "30")
        s = Settings()
        assert s.screen_capture_interval_seconds == 30
