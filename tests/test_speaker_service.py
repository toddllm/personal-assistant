from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
import numpy as np

from speaker_service.config import Settings
from speaker_service.service import create_app


def build_settings(tmp_path) -> Settings:
    return Settings(
        profiles_db_path=tmp_path / "speaker_profiles.db",
        log_file_path=tmp_path / "speaker-service.log",
        min_voice_dbfs=-60.0,
        enrollment_min_voice_seconds=1.0,
        profile_similarity_threshold=0.86,
        verification_threshold=0.86,
    )


def make_voice(seed: int, duration: float = 6.0, variant: float = 0.0, sample_rate: int = 16000) -> np.ndarray:
    rng = np.random.RandomState(seed)
    t = np.linspace(0.0, duration, int(sample_rate * duration), endpoint=False)
    base = 180.0 + (seed * 17.0) + (variant * 5.0)
    envelope = 0.55 + (0.35 * np.sin(2 * np.pi * (2.1 + (0.07 * seed)) * t))
    signal = np.zeros_like(t)
    for harmonic in range(1, 6):
        freq = base * harmonic * (1.0 + (0.002 * rng.randn()))
        phase = rng.rand() * np.pi * 2.0
        amplitude = (1.0 / harmonic) * (0.8 + (0.4 * rng.rand()))
        signal += amplitude * np.sin(2 * np.pi * freq * t + phase)
    signal *= envelope
    signal += 0.02 * rng.randn(t.size)
    signal /= np.max(np.abs(signal)) + 1e-6
    return (signal * 0.65).astype(np.float32)


def pcm_base64(samples: np.ndarray) -> str:
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = np.where(clipped < 0, clipped * 0x8000, clipped * 0x7FFF).astype(np.int16)
    return base64.b64encode(pcm.tobytes()).decode("ascii")


def diarize_payload(samples: np.ndarray, *, source_id: str = "desk-mic") -> dict[str, object]:
    start = datetime.now(tz=UTC)
    end = start + timedelta(seconds=float(len(samples)) / 16000.0)
    return {
        "source_id": source_id,
        "session_id": "session-1",
        "sample_rate": 16000,
        "channels": 1,
        "started_at": start.isoformat(),
        "ended_at": end.isoformat(),
        "pcm_s16le_base64": pcm_base64(samples),
    }


def test_enroll_list_delete_profiles(tmp_path) -> None:
    settings = build_settings(tmp_path)
    app = create_app(settings)
    chelsea = make_voice(3)

    with TestClient(app) as client:
        created = client.post(
            "/v1/enroll",
            json={
                "name": "Chelsea Deshane",
                "sample_rate": 16000,
                "channels": 1,
                "pcm_s16le_base64": pcm_base64(chelsea),
            },
        )
        assert created.status_code == 201
        assert created.json()["name"] == "Chelsea Deshane"
        assert created.json()["sample_count"] == 1

        updated = client.post(
            "/v1/enroll",
            json={
                "name": "Chelsea Deshane",
                "sample_rate": 16000,
                "channels": 1,
                "pcm_s16le_base64": pcm_base64(make_voice(3, variant=0.8)),
            },
        )
        assert updated.status_code == 200
        assert updated.json()["sample_count"] == 2

        listed = client.get("/v1/profiles")
        assert listed.status_code == 200
        assert [item["name"] for item in listed.json()] == ["Chelsea Deshane"]

        deleted = client.delete(f"/v1/profiles/{updated.json()['id']}")
        assert deleted.status_code == 204

        empty = client.get("/v1/profiles")
        assert empty.status_code == 200
        assert empty.json() == []


def test_verify_and_named_diarization(tmp_path) -> None:
    settings = build_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        enrolled = client.post(
            "/v1/enroll",
            json={
                "name": "Chelsea Deshane",
                "sample_rate": 16000,
                "channels": 1,
                "pcm_s16le_base64": pcm_base64(make_voice(5)),
            },
        )
        assert enrolled.status_code == 201

        verify = client.post(
            "/v1/verify",
            json={
                "sample_rate": 16000,
                "channels": 1,
                "pcm_s16le_base64": pcm_base64(make_voice(5, variant=0.7)),
            },
        )
        assert verify.status_code == 200
        payload = verify.json()
        assert payload["matches"][0]["name"] == "Chelsea Deshane"
        assert payload["matches"][0]["is_match"] is True
        assert payload["matches"][0]["similarity"] >= payload["threshold"]

        diarized = client.post("/v1/diarize/chunk", json=diarize_payload(make_voice(5, variant=0.4)))
        assert diarized.status_code == 200
        assert diarized.json()["speaker"] == "Chelsea Deshane"


def test_unknown_voice_falls_back_to_cluster_labels(tmp_path) -> None:
    settings = build_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        first = client.post("/v1/diarize/chunk", json=diarize_payload(make_voice(1), source_id="family-room"))
        second = client.post("/v1/diarize/chunk", json=diarize_payload(make_voice(9), source_id="family-room"))

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["speaker"] == "SPK_01"
    assert second.json()["speaker"] in {"SPK_01", "SPK_02"}
