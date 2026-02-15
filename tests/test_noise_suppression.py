"""Tests for the noise suppression module."""

from __future__ import annotations

import numpy as np

from audio_assist.noise_suppression import NoiseSuppressor


def _make_pcm(duration_s: float = 0.5, sample_rate: int = 16000, freq: float = 440.0) -> bytes:
    """Generate a sine wave as PCM s16le bytes."""
    t = np.linspace(0, duration_s, int(sample_rate * duration_s), endpoint=False)
    signal = (np.sin(2 * np.pi * freq * t) * 16000).astype(np.int16)
    return signal.tobytes()


def _make_silence(duration_s: float = 0.5, sample_rate: int = 16000) -> bytes:
    """Generate silence as PCM s16le bytes."""
    samples = int(sample_rate * duration_s)
    return np.zeros(samples, dtype=np.int16).tobytes()


class TestShouldSuppress:
    def test_suppresses_desk_mic(self):
        ns = NoiseSuppressor(enabled=True)
        assert ns.should_suppress("desk-mic") is True
        assert ns.should_suppress("desk-mic-manual") is True

    def test_does_not_suppress_other_mic_sources(self):
        """Only desk-mic prefix gets suppression; bose-mic and others are skipped."""
        ns = NoiseSuppressor(enabled=True)
        assert ns.should_suppress("bose-mic") is False
        assert ns.should_suppress("usb-mic") is False
        assert ns.should_suppress("microphone-1") is False

    def test_does_not_suppress_system_audio(self):
        ns = NoiseSuppressor(enabled=True)
        assert ns.should_suppress("system-audio") is False
        assert ns.should_suppress("system-audio-blackhole") is False

    def test_does_not_suppress_when_disabled(self):
        ns = NoiseSuppressor(enabled=False)
        assert ns.should_suppress("desk-mic") is False

    def test_custom_prefix(self):
        ns = NoiseSuppressor(enabled=True, mic_source_prefix="custom-")
        assert ns.should_suppress("custom-mic") is True
        assert ns.should_suppress("desk-mic") is False  # only prefix match, no keyword fallback


class TestSuppress:
    def test_returns_bytes_for_mic_source(self):
        ns = NoiseSuppressor(enabled=True)
        pcm = _make_pcm()
        result = ns.suppress(pcm, 16000, "desk-mic")
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_passthrough_for_system_audio(self):
        ns = NoiseSuppressor(enabled=True)
        pcm = _make_pcm()
        result = ns.suppress(pcm, 16000, "system-audio")
        assert result == pcm

    def test_passthrough_when_disabled(self):
        ns = NoiseSuppressor(enabled=False)
        pcm = _make_pcm()
        result = ns.suppress(pcm, 16000, "desk-mic")
        assert result == pcm

    def test_silent_audio_skipped(self):
        ns = NoiseSuppressor(enabled=True)
        silence = _make_silence()
        result = ns.suppress(silence, 16000, "desk-mic")
        assert result == silence

    def test_empty_audio_returns_unchanged(self):
        ns = NoiseSuppressor(enabled=True)
        result = ns.suppress(b"", 16000, "desk-mic")
        assert result == b""

    def test_output_length_matches_input(self):
        ns = NoiseSuppressor(enabled=True)
        pcm = _make_pcm(duration_s=1.0)
        result = ns.suppress(pcm, 16000, "desk-mic")
        assert len(result) == len(pcm)


class TestStats:
    def test_stats_track_suppressed_segments(self):
        ns = NoiseSuppressor(enabled=True)
        pcm = _make_pcm()
        ns.suppress(pcm, 16000, "desk-mic")
        status = ns.status()
        assert status["segments_processed"] == 1
        assert status["segments_suppressed"] == 1
        assert status["segments_skipped"] == 0

    def test_stats_track_skipped_segments(self):
        ns = NoiseSuppressor(enabled=True)
        pcm = _make_pcm()
        ns.suppress(pcm, 16000, "system-audio")
        status = ns.status()
        assert status["segments_processed"] == 0
        assert status["segments_skipped"] == 1

    def test_status_returns_expected_keys(self):
        ns = NoiseSuppressor(enabled=True)
        status = ns.status()
        assert "enabled" in status
        assert "available" in status
        assert "segments_processed" in status
        assert "avg_snr_improvement_db" in status


class TestProperties:
    def test_available_reflects_noisereduce_import(self):
        ns = NoiseSuppressor(enabled=True)
        # noisereduce is installed, so should be True
        assert ns.available is True

    def test_enabled_property(self):
        ns = NoiseSuppressor(enabled=True)
        assert ns.enabled is True

    def test_prop_decrease_clamped(self):
        ns = NoiseSuppressor(enabled=True, prop_decrease=2.0)
        assert ns._prop_decrease == 1.0
        ns2 = NoiseSuppressor(enabled=True, prop_decrease=-1.0)
        assert ns2._prop_decrease == 0.0
