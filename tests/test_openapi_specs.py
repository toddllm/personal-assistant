"""Tests for OpenAPI specification files."""

from pathlib import Path

import pytest
import yaml

SPECS_DIR = Path(__file__).resolve().parent.parent / "docs" / "api-specs"
SPEAKER_SPEC = SPECS_DIR / "speaker-enrollment.yaml"
SCREEN_SPEC = SPECS_DIR / "screen-capture.yaml"


def _load_spec(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


# ── Speaker Enrollment Spec ──────────────────────────────────────────


class TestSpeakerEnrollmentSpec:
    """Validate speaker-enrollment.yaml structure and content."""

    def test_file_exists(self):
        assert SPEAKER_SPEC.exists()

    def test_valid_yaml(self):
        spec = _load_spec(SPEAKER_SPEC)
        assert isinstance(spec, dict)

    def test_openapi_version(self):
        spec = _load_spec(SPEAKER_SPEC)
        assert spec["openapi"] == "3.1.0"

    def test_server_port_8791(self):
        spec = _load_spec(SPEAKER_SPEC)
        urls = [s["url"] for s in spec["servers"]]
        assert any("8791" in u for u in urls)

    def test_required_paths_present(self):
        spec = _load_spec(SPEAKER_SPEC)
        paths = set(spec["paths"].keys())
        assert "/v1/enroll" in paths
        assert "/v1/profiles" in paths
        assert "/v1/profiles/{profile_id}" in paths
        assert "/v1/verify" in paths

    def test_enroll_is_post(self):
        spec = _load_spec(SPEAKER_SPEC)
        assert "post" in spec["paths"]["/v1/enroll"]

    def test_profiles_list_is_get(self):
        spec = _load_spec(SPEAKER_SPEC)
        assert "get" in spec["paths"]["/v1/profiles"]

    def test_profile_delete_method(self):
        spec = _load_spec(SPEAKER_SPEC)
        assert "delete" in spec["paths"]["/v1/profiles/{profile_id}"]

    def test_schemas_exclude_embedding_blob(self):
        spec = _load_spec(SPEAKER_SPEC)
        profile_schema = spec["components"]["schemas"]["SpeakerProfile"]
        prop_names = set(profile_schema["properties"].keys())
        assert "embedding_blob" not in prop_names


# ── Screen Capture Spec ──────────────────────────────────────────────


class TestScreenCaptureSpec:
    """Validate screen-capture.yaml structure and content."""

    def test_file_exists(self):
        assert SCREEN_SPEC.exists()

    def test_valid_yaml(self):
        spec = _load_spec(SCREEN_SPEC)
        assert isinstance(spec, dict)

    def test_openapi_version(self):
        spec = _load_spec(SCREEN_SPEC)
        assert spec["openapi"] == "3.1.0"

    def test_server_port_8794(self):
        spec = _load_spec(SCREEN_SPEC)
        urls = [s["url"] for s in spec["servers"]]
        assert any("8794" in u for u in urls)

    def test_required_paths_present(self):
        spec = _load_spec(SCREEN_SPEC)
        paths = set(spec["paths"].keys())
        assert "/health" in paths
        assert "/v1/capture" in paths
        assert "/v1/captures/{session_id}" in paths
        assert "/v1/captures/latest" in paths

    def test_capture_is_post(self):
        spec = _load_spec(SCREEN_SPEC)
        assert "post" in spec["paths"]["/v1/capture"]

    def test_captures_session_get(self):
        spec = _load_spec(SCREEN_SPEC)
        assert "get" in spec["paths"]["/v1/captures/{session_id}"]

    def test_captures_session_delete(self):
        spec = _load_spec(SCREEN_SPEC)
        assert "delete" in spec["paths"]["/v1/captures/{session_id}"]

    def test_health_is_get(self):
        spec = _load_spec(SCREEN_SPEC)
        assert "get" in spec["paths"]["/health"]
