"""Tests for speaker enrollment CLI (audio_assist.enroll).

Uses FakeExtractor (deterministic 192-dim vectors) and _make_wav() helper
so no speechbrain/torchaudio dependency is needed.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from audio_assist.enroll import (
    EMBEDDING_BYTES,
    EMBEDDING_DIM,
    AudioValidation,
    ProfileStore,
    SpeakerProfile,
    StoredSpeakerProfile,
    blob_to_embedding,
    embedding_to_blob,
    enroll_speaker,
    main,
    validate_wav,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeExtractor:
    """Deterministic extractor that returns a normalised vector seeded from the filename."""

    def __init__(self, *args, **kwargs):
        pass

    def extract(self, wav_path: Path) -> np.ndarray:
        seed = sum(wav_path.name.encode())
        rng = np.random.RandomState(seed)
        vec = rng.randn(EMBEDDING_DIM).astype(np.float32)
        vec /= np.linalg.norm(vec)
        return vec


def _make_wav(
    path: Path,
    duration: float = 15.0,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
    silent: bool = False,
    frequency: float = 440.0,
) -> Path:
    """Create a synthetic WAV file with a sine tone (or silence)."""
    n_frames = int(sample_rate * duration)
    if silent:
        samples = np.zeros(n_frames * channels, dtype=np.int16)
    else:
        t = np.linspace(0, duration, n_frames, endpoint=False)
        tone = (np.sin(2 * np.pi * frequency * t) * 16000).astype(np.int16)
        if channels > 1:
            tone = np.column_stack([tone] * channels).flatten()
        samples = tone

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())
    return path


# ---------------------------------------------------------------------------
# TestValidateWav
# ---------------------------------------------------------------------------


class TestValidateWav:
    def test_valid_wav(self, tmp_path: Path):
        wav = _make_wav(tmp_path / "good.wav", duration=15.0)
        result = validate_wav(wav)
        assert result.valid is True
        assert result.duration == pytest.approx(15.0, abs=0.1)
        assert result.sample_rate == 16000
        assert result.channels == 1

    def test_too_short(self, tmp_path: Path):
        wav = _make_wav(tmp_path / "short.wav", duration=5.0)
        result = validate_wav(wav)
        assert result.valid is False
        assert "short" in result.reason.lower()

    def test_too_long(self, tmp_path: Path):
        wav = _make_wav(tmp_path / "long.wav", duration=35.0)
        result = validate_wav(wav)
        assert result.valid is False
        assert "long" in result.reason.lower()

    def test_silent(self, tmp_path: Path):
        wav = _make_wav(tmp_path / "silent.wav", duration=15.0, silent=True)
        result = validate_wav(wav)
        assert result.valid is False
        assert "silent" in result.reason.lower()

    def test_missing_file(self, tmp_path: Path):
        result = validate_wav(tmp_path / "nonexistent.wav")
        assert result.valid is False
        assert "exist" in result.reason.lower()

    def test_wrong_extension(self, tmp_path: Path):
        p = tmp_path / "audio.mp3"
        p.write_bytes(b"\x00" * 100)
        result = validate_wav(p)
        assert result.valid is False
        assert ".wav" in result.reason

    def test_bad_sample_width(self, tmp_path: Path):
        """8-bit WAV should be rejected (we require 16-bit)."""
        p = tmp_path / "eight_bit.wav"
        n_frames = 16000 * 15
        samples = np.full(n_frames, 128, dtype=np.uint8)
        with wave.open(str(p), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(1)  # 8-bit
            wf.setframerate(16000)
            wf.writeframes(samples.tobytes())
        result = validate_wav(p)
        assert result.valid is False
        assert "16-bit" in result.reason or "8-bit" in result.reason


# ---------------------------------------------------------------------------
# TestEmbeddingSerialization
# ---------------------------------------------------------------------------


class TestEmbeddingSerialization:
    def test_round_trip(self):
        vec = np.random.randn(EMBEDDING_DIM).astype(np.float32)
        blob = embedding_to_blob(vec)
        recovered = blob_to_embedding(blob)
        np.testing.assert_array_almost_equal(vec, recovered)

    def test_wrong_dim(self):
        vec = np.random.randn(100).astype(np.float32)
        with pytest.raises(ValueError, match="Expected shape"):
            embedding_to_blob(vec)

    def test_wrong_blob_size(self):
        blob = b"\x00" * 100
        with pytest.raises(ValueError, match="Expected.*bytes"):
            blob_to_embedding(blob)


# ---------------------------------------------------------------------------
# TestProfileStore
# ---------------------------------------------------------------------------


class TestProfileStore:
    def _make_embedding(self, seed: int = 42) -> np.ndarray:
        rng = np.random.RandomState(seed)
        vec = rng.randn(EMBEDDING_DIM).astype(np.float32)
        vec /= np.linalg.norm(vec)
        return vec

    def test_insert(self):
        store = ProfileStore()
        emb = self._make_embedding()
        profile = store.upsert("Alice", emb)
        assert profile.name == "Alice"
        assert profile.sample_count == 1
        assert profile.embedding_dim == EMBEDDING_DIM
        store.close()

    def test_upsert_averages(self):
        store = ProfileStore()
        emb1 = self._make_embedding(1)
        emb2 = self._make_embedding(2)
        store.upsert("Bob", emb1)
        profile = store.upsert("Bob", emb2)
        assert profile.sample_count == 2
        store.close()

    def test_list_all(self):
        store = ProfileStore()
        store.upsert("A", self._make_embedding(1))
        store.upsert("B", self._make_embedding(2))
        profiles = store.list_all()
        assert len(profiles) == 2
        assert profiles[0].name == "A"
        assert profiles[1].name == "B"
        store.close()

    def test_get_by_id(self):
        store = ProfileStore()
        p = store.upsert("Carol", self._make_embedding())
        fetched = store.get_by_id(p.id)
        assert fetched is not None
        assert fetched.name == "Carol"
        store.close()

    def test_get_by_id_missing(self):
        store = ProfileStore()
        assert store.get_by_id(999) is None
        store.close()

    def test_get_by_name(self):
        store = ProfileStore()
        profile = store.upsert("Chelsea Deshane", self._make_embedding())
        fetched = store.get_by_name("Chelsea Deshane")
        assert fetched is not None
        assert fetched.id == profile.id
        store.close()

    def test_delete(self):
        store = ProfileStore()
        p = store.upsert("Del", self._make_embedding())
        assert store.delete(p.id) is True
        assert store.get_by_id(p.id) is None
        assert store.delete(999) is False
        store.close()

    def test_has_owner(self):
        store = ProfileStore()
        assert store.has_owner() is False
        store.upsert("Owner", self._make_embedding(), is_owner=True)
        assert store.has_owner() is True
        store.close()

    def test_auto_owner_first_profile(self):
        store = ProfileStore()
        p = store.upsert("First", self._make_embedding())
        assert p.is_owner is True
        p2 = store.upsert("Second", self._make_embedding(2))
        assert p2.is_owner is False
        store.close()

    def test_upsert_preserves_owner_flag(self):
        store = ProfileStore()
        store.upsert("Owner", self._make_embedding(1), is_owner=True)
        updated = store.upsert("Owner", self._make_embedding(2), is_owner=False)
        assert updated.is_owner is True
        store.close()

    def test_list_all_with_embeddings(self):
        store = ProfileStore()
        emb = self._make_embedding()
        profile = store.upsert("Chelsea", emb)
        rows = store.list_all_with_embeddings()
        assert len(rows) == 1
        item = rows[0]
        assert isinstance(item, StoredSpeakerProfile)
        assert item.profile.id == profile.id
        np.testing.assert_allclose(item.embedding, emb, atol=1e-5)
        store.close()


# ---------------------------------------------------------------------------
# TestEnrollSpeaker
# ---------------------------------------------------------------------------


class TestEnrollSpeaker:
    def test_happy_path(self, tmp_path: Path):
        wav = _make_wav(tmp_path / "s1.wav")
        store = ProfileStore()
        ext = FakeExtractor()
        profile = enroll_speaker(
            "Test", [wav], extractor=ext, store=store
        )
        assert profile.name == "Test"
        assert profile.sample_count == 1
        store.close()

    def test_multiple_samples(self, tmp_path: Path):
        wavs = [_make_wav(tmp_path / f"s{i}.wav") for i in range(3)]
        store = ProfileStore()
        ext = FakeExtractor()
        profile = enroll_speaker(
            "Multi", wavs, extractor=ext, store=store
        )
        assert profile.name == "Multi"
        assert profile.sample_count == 1  # single enrollment call
        store.close()

    def test_too_few_samples(self, tmp_path: Path):
        with pytest.raises(ValueError, match="At least one"):
            enroll_speaker("Empty", [], extractor=FakeExtractor(), store=ProfileStore())

    def test_too_many_samples(self, tmp_path: Path):
        paths = [tmp_path / f"s{i}.wav" for i in range(21)]
        with pytest.raises(ValueError, match="Too many"):
            enroll_speaker("Spam", paths, extractor=FakeExtractor(), store=ProfileStore())

    def test_invalid_audio(self, tmp_path: Path):
        wav = _make_wav(tmp_path / "bad.wav", duration=5.0)
        with pytest.raises(ValueError, match="Invalid audio"):
            enroll_speaker(
                "Bad", [wav], extractor=FakeExtractor(), store=ProfileStore()
            )

    def test_explicit_owner(self, tmp_path: Path):
        wav = _make_wav(tmp_path / "owner.wav")
        store = ProfileStore()
        # Insert a non-owner first so auto-owner fires for it
        ext = FakeExtractor()
        store.upsert("First", ext.extract(wav))
        # Now enroll with explicit owner
        wav2 = _make_wav(tmp_path / "owner2.wav")
        profile = enroll_speaker(
            "Boss", [wav2], is_owner=True, extractor=ext, store=store
        )
        assert profile.is_owner is True
        store.close()


# ---------------------------------------------------------------------------
# TestCLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_dir_input(self, tmp_path: Path, capsys):
        wav_dir = tmp_path / "samples"
        wav_dir.mkdir()
        _make_wav(wav_dir / "a.wav")
        _make_wav(wav_dir / "b.wav")
        db = str(tmp_path / "test.db")

        with patch("audio_assist.enroll.SpeechBrainExtractor", FakeExtractor):
            main(["--name", "CLI_Dir", "--samples", str(wav_dir), "--db", db])

        out = capsys.readouterr().out
        assert "CLI_Dir" in out
        assert "id=" in out

    def test_comma_separated(self, tmp_path: Path, capsys):
        w1 = _make_wav(tmp_path / "x.wav")
        w2 = _make_wav(tmp_path / "y.wav")
        db = str(tmp_path / "test.db")

        with patch("audio_assist.enroll.SpeechBrainExtractor", FakeExtractor):
            main(["--name", "CLI_CSV", "--samples", f"{w1},{w2}", "--db", db])

        out = capsys.readouterr().out
        assert "CLI_CSV" in out

    def test_missing_args(self):
        with pytest.raises(SystemExit):
            main([])
