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


def test_sources_ensure_disabled_returns_expected_payload(tmp_path) -> None:
    settings = build_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post("/v1/sources/ensure")
        assert response.status_code == 200
        payload = response.json()

    assert payload["enabled"] is False
    assert payload["started_count"] == 0
    assert payload["running_count"] == 0
    assert payload["errors"] == []
    assert payload["sources"] == []
