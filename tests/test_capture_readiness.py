from __future__ import annotations

from fastapi.testclient import TestClient

from audio_assist.config import Settings
from audio_assist.service import create_app


def build_settings(tmp_path) -> Settings:
    return Settings(
        database_path=tmp_path / "audio_assist_test.db",
        archive_audio=False,
        speaker_enabled=False,
        calendar_match_enabled=False,
        google_sync_autostart=False,
        capture_autostart_enabled=False,
        capture_watchdog_enabled=False,
        tts_enabled=False,
    )


def test_capture_readiness_reports_down_when_no_running_sources(tmp_path) -> None:
    settings = build_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/v1/capture/readiness")
        assert response.status_code == 200
        payload = response.json()

    assert payload["status"] == "down"
    issue_codes = {item["code"] for item in payload["issues"]}
    assert "no_running_sources" in issue_codes
    assert payload["signals"]["running_source_count"] == 0
