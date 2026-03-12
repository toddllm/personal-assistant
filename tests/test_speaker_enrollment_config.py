"""Tests for speaker verification (voice enrollment) config fields."""

import os

import pytest

from audio_assist.config import Settings


class TestSpeakerVerificationConfig:
    """Verify speaker verification config defaults and env overrides."""

    def test_speaker_verification_disabled_by_default(self):
        s = Settings()
        assert s.speaker_verification_enabled is False

    def test_speaker_embedding_model_default(self):
        s = Settings()
        assert s.speaker_embedding_model == "speechbrain/spkrec-ecapa-voxceleb"

    def test_speaker_similarity_threshold_default(self):
        s = Settings()
        assert s.speaker_similarity_threshold == pytest.approx(0.25)

    def test_owner_speaker_profile_id_default_none(self):
        s = Settings()
        assert s.owner_speaker_profile_id is None

    def test_env_override_verification_enabled(self, monkeypatch):
        monkeypatch.setenv("AUDIO_ASSIST_SPEAKER_VERIFICATION_ENABLED", "true")
        s = Settings()
        assert s.speaker_verification_enabled is True

    def test_env_override_embedding_model(self, monkeypatch):
        monkeypatch.setenv("AUDIO_ASSIST_SPEAKER_EMBEDDING_MODEL", "pyannote/embedding")
        s = Settings()
        assert s.speaker_embedding_model == "pyannote/embedding"

    def test_env_override_similarity_threshold(self, monkeypatch):
        monkeypatch.setenv("AUDIO_ASSIST_SPEAKER_SIMILARITY_THRESHOLD", "0.40")
        s = Settings()
        assert s.speaker_similarity_threshold == pytest.approx(0.40)
