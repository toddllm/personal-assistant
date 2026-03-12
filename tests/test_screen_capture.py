"""Tests for screen capture service (screen_capture package).

Uses FakeCaptureBackend (synthetic PNG via Pillow) + FastAPI TestClient.
tmp_path for storage.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from screen_capture.capture import CaptureBackend, get_backend
from screen_capture.config import Settings
from screen_capture.schemas import (
    CaptureListResponse,
    CaptureRecord,
    CaptureRequest,
    DeleteResponse,
    ErrorResponse,
    HealthResponse,
)
from screen_capture.service import create_app
from screen_capture.storage import CaptureStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeCaptureBackend:
    """Returns a synthetic 100x80 PNG image."""

    def capture(self) -> tuple[bytes, int, int]:
        img = Image.new("RGB", (100, 80), color=(255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), 100, 80


class FailingBackend:
    """Backend that always raises RuntimeError."""

    def capture(self) -> tuple[bytes, int, int]:
        raise RuntimeError("No display available")


def _make_store(tmp_path: Path) -> CaptureStore:
    db = tmp_path / "test.db"
    cap_dir = tmp_path / "captures"
    cap_dir.mkdir()
    return CaptureStore(db_path=db, capture_dir=cap_dir)


def _make_client(tmp_path: Path, **kwargs) -> TestClient:
    settings = Settings(
        database_path=tmp_path / "test.db",
        capture_dir=tmp_path / "captures",
        max_frames_per_session=5,
    )
    store = _make_store(tmp_path)
    backend = kwargs.pop("backend", FakeCaptureBackend())
    app = create_app(settings, backend=backend, store=store)
    return TestClient(app)


# ---------------------------------------------------------------------------
# TestScreenCaptureServiceConfig
# ---------------------------------------------------------------------------


class TestScreenCaptureServiceConfig:
    def test_defaults(self):
        s = Settings()
        assert s.host == "127.0.0.1"
        assert s.port == 8794
        assert s.capture_quality == 50
        assert s.capture_scale == pytest.approx(0.5)

    def test_env_override_port(self, monkeypatch):
        monkeypatch.setenv("SCREEN_CAPTURE_PORT", "9999")
        s = Settings()
        assert s.port == 9999

    def test_env_override_quality(self, monkeypatch):
        monkeypatch.setenv("SCREEN_CAPTURE_CAPTURE_QUALITY", "80")
        s = Settings()
        assert s.capture_quality == 80

    def test_env_override_max_frames(self, monkeypatch):
        monkeypatch.setenv("SCREEN_CAPTURE_MAX_FRAMES_PER_SESSION", "500")
        s = Settings()
        assert s.max_frames_per_session == 500


# ---------------------------------------------------------------------------
# TestCaptureStore
# ---------------------------------------------------------------------------


class TestCaptureStore:
    def test_save_capture(self, tmp_path: Path):
        store = _make_store(tmp_path)
        img_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        record = store.save_capture("sess-1", img_data, 800, 600)
        assert record.session_id == "sess-1"
        assert record.width == 800
        assert record.height == 600
        assert record.file_size_bytes == len(img_data)
        store.close()

    def test_list_session(self, tmp_path: Path):
        store = _make_store(tmp_path)
        store.save_capture("s1", b"img1", 100, 100)
        store.save_capture("s1", b"img2", 100, 100)
        store.save_capture("s2", b"img3", 100, 100)
        captures, total = store.list_session("s1")
        assert total == 2
        assert len(captures) == 2
        store.close()

    def test_get_latest(self, tmp_path: Path):
        store = _make_store(tmp_path)
        store.save_capture("s1", b"first", 100, 100)
        store.save_capture("s1", b"second", 200, 200)
        latest = store.get_latest()
        assert latest is not None
        assert latest.width == 200
        store.close()

    def test_delete_session_and_files(self, tmp_path: Path):
        store = _make_store(tmp_path)
        store.save_capture("del-me", b"data", 100, 100)
        session_dir = tmp_path / "captures" / "del-me"
        assert session_dir.exists()
        deleted = store.delete_session("del-me")
        assert deleted == 1
        assert not session_dir.exists()
        store.close()

    def test_session_frame_count(self, tmp_path: Path):
        store = _make_store(tmp_path)
        assert store.session_frame_count("empty") == 0
        store.save_capture("fc", b"a", 10, 10)
        store.save_capture("fc", b"b", 10, 10)
        assert store.session_frame_count("fc") == 2
        store.close()

    def test_empty_latest(self, tmp_path: Path):
        store = _make_store(tmp_path)
        assert store.get_latest() is None
        store.close()

    def test_pagination(self, tmp_path: Path):
        store = _make_store(tmp_path)
        for i in range(5):
            store.save_capture("pg", f"img{i}".encode(), 10, 10)
        captures, total = store.list_session("pg", limit=2, offset=0)
        assert total == 5
        assert len(captures) == 2
        captures2, _ = store.list_session("pg", limit=2, offset=2)
        assert len(captures2) == 2
        store.close()

    def test_idempotent_migrations(self, tmp_path: Path):
        """Running migrations twice should not error."""
        store = _make_store(tmp_path)
        store._run_migrations()
        store.save_capture("ok", b"data", 10, 10)
        store.close()

    def test_delete_empty_session(self, tmp_path: Path):
        store = _make_store(tmp_path)
        assert store.delete_session("nonexistent") == 0
        store.close()


# ---------------------------------------------------------------------------
# TestServiceEndpoints
# ---------------------------------------------------------------------------


class TestServiceEndpoints:
    def test_health(self, tmp_path: Path):
        client = _make_client(tmp_path)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "uptime_seconds" in data

    def test_capture_201(self, tmp_path: Path):
        client = _make_client(tmp_path)
        resp = client.post("/v1/capture", json={"session_id": "test-sess"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["session_id"] == "test-sess"
        assert data["width"] > 0
        assert data["height"] > 0

    def test_list_session(self, tmp_path: Path):
        client = _make_client(tmp_path)
        client.post("/v1/capture", json={"session_id": "ls"})
        client.post("/v1/capture", json={"session_id": "ls"})
        resp = client.get("/v1/captures/ls")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["captures"]) == 2

    def test_latest_capture(self, tmp_path: Path):
        client = _make_client(tmp_path)
        client.post("/v1/capture", json={"session_id": "lt"})
        resp = client.get("/v1/captures/latest")
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "lt"

    def test_delete_session(self, tmp_path: Path):
        client = _make_client(tmp_path)
        client.post("/v1/capture", json={"session_id": "del"})
        resp = client.delete("/v1/captures/del")
        assert resp.status_code == 200
        assert resp.json()["deleted_count"] == 1

    def test_latest_404(self, tmp_path: Path):
        client = _make_client(tmp_path)
        resp = client.get("/v1/captures/latest")
        assert resp.status_code == 404

    def test_delete_404(self, tmp_path: Path):
        client = _make_client(tmp_path)
        resp = client.delete("/v1/captures/nonexistent")
        assert resp.status_code == 404

    def test_capture_429_frame_limit(self, tmp_path: Path):
        client = _make_client(tmp_path)
        for _ in range(5):
            resp = client.post("/v1/capture", json={"session_id": "full"})
            assert resp.status_code == 201
        resp = client.post("/v1/capture", json={"session_id": "full"})
        assert resp.status_code == 429

    def test_capture_503_backend_failure(self, tmp_path: Path):
        client = _make_client(tmp_path, backend=FailingBackend())
        resp = client.post("/v1/capture", json={"session_id": "fail"})
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# TestCaptureBackendSelection
# ---------------------------------------------------------------------------


class TestCaptureBackendSelection:
    def test_priority_returns_something(self):
        # In test env we may or may not have backends; just verify no crash
        # if both are missing
        try:
            backend = get_backend()
            assert isinstance(backend, CaptureBackend)
        except ImportError:
            pass  # Expected if neither PyObjC nor mss installed

    def test_cg_preferred_on_macos(self):
        """CoreGraphics should be preferred when Quartz is available."""
        quartz_mock = type("Quartz", (), {})()
        with patch.dict("sys.modules", {"Quartz": quartz_mock}):
            from screen_capture.capture import get_backend as gb
            try:
                backend = gb()
                # If Quartz is importable, we get CoreGraphicsBackend
                from screen_capture.capture import CoreGraphicsBackend
                assert isinstance(backend, CoreGraphicsBackend)
            except ImportError:
                pass  # Quartz mock insufficient for full init

    def test_mss_fallback(self):
        """mss should be used when Quartz is not available."""
        with patch.dict("sys.modules", {"Quartz": None}):
            try:
                import mss  # noqa: F401
                from screen_capture.capture import get_backend as gb2
                backend = gb2()
                from screen_capture.capture import MSSBackend
                assert isinstance(backend, MSSBackend)
            except ImportError:
                pass  # mss not installed either


# ---------------------------------------------------------------------------
# TestSchemas
# ---------------------------------------------------------------------------


class TestSchemas:
    def test_capture_request_validation(self):
        req = CaptureRequest(session_id="test")
        assert req.session_id == "test"
        assert req.quality is None

    def test_capture_request_bounds(self):
        with pytest.raises(Exception):
            CaptureRequest(session_id="test", quality=0)
        with pytest.raises(Exception):
            CaptureRequest(session_id="test", quality=101)
        with pytest.raises(Exception):
            CaptureRequest(session_id="test", scale=0.0)

    def test_capture_record_serialization(self):
        record = CaptureRecord(
            id=1,
            session_id="s1",
            captured_at="2026-01-01T00:00:00Z",
            file_path="captures/s1/img.webp",
            width=800,
            height=600,
            file_size_bytes=1234,
        )
        data = record.model_dump()
        assert data["id"] == 1
        assert data["ocr_text"] is None
        assert data["participants_detected"] is None
