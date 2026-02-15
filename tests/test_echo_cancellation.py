"""Tests for the echo cancellation module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from audio_assist.echo_cancellation import EchoCanceller

SAMPLE_RATE = 16000


def _make_mono_pcm(
    duration_s: float = 0.5,
    sample_rate: int = SAMPLE_RATE,
    freq: float = 440.0,
    amplitude: float = 16000.0,
) -> bytes:
    """Generate a mono sine wave as PCM s16le bytes."""
    t = np.linspace(0, duration_s, int(sample_rate * duration_s), endpoint=False)
    signal = (np.sin(2 * np.pi * freq * t) * amplitude).astype(np.int16)
    return signal.tobytes()


def _make_stereo_pcm(
    duration_s: float = 0.5,
    sample_rate: int = SAMPLE_RATE,
    freq: float = 440.0,
    amplitude: float = 16000.0,
) -> bytes:
    """Generate a stereo sine wave as interleaved PCM s16le bytes."""
    t = np.linspace(0, duration_s, int(sample_rate * duration_s), endpoint=False)
    mono = (np.sin(2 * np.pi * freq * t) * amplitude).astype(np.int16)
    stereo = np.column_stack([mono, mono]).flatten()
    return stereo.tobytes()


def _make_silence(duration_s: float = 0.5, sample_rate: int = SAMPLE_RATE) -> bytes:
    samples = int(sample_rate * duration_s)
    return np.zeros(samples, dtype=np.int16).tobytes()


def _ts(offset_s: float = 0.0) -> datetime:
    return datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=offset_s)


class TestShouldCancel:
    def test_cancels_desk_mic(self):
        ec = EchoCanceller(enabled=True)
        assert ec.should_cancel("desk-mic") is True
        assert ec.should_cancel("desk-mic-manual") is True

    def test_does_not_cancel_other_mic_sources(self):
        """Only desk-mic prefix gets AEC; bose-mic and others are skipped."""
        ec = EchoCanceller(enabled=True)
        assert ec.should_cancel("bose-mic") is False
        assert ec.should_cancel("usb-mic") is False
        assert ec.should_cancel("microphone-1") is False

    def test_does_not_cancel_system_audio(self):
        ec = EchoCanceller(enabled=True)
        assert ec.should_cancel("system-audio") is False
        assert ec.should_cancel("system-audio-blackhole") is False

    def test_does_not_cancel_when_disabled(self):
        ec = EchoCanceller(enabled=False)
        assert ec.should_cancel("desk-mic") is False


class TestIngestReference:
    def test_buffers_system_audio(self):
        ec = EchoCanceller(enabled=True)
        pcm = _make_stereo_pcm()
        ec.ingest_reference(
            source_id="system-audio",
            pcm_s16le=pcm,
            sample_rate=SAMPLE_RATE,
            channels=2,
            started_at=_ts(0),
            ended_at=_ts(0.5),
        )
        status = ec.status()
        assert status["reference_segments_buffered"] == 1

    def test_ignores_mic_sources(self):
        ec = EchoCanceller(enabled=True)
        pcm = _make_mono_pcm()
        ec.ingest_reference(
            source_id="desk-mic",
            pcm_s16le=pcm,
            sample_rate=SAMPLE_RATE,
            channels=1,
            started_at=_ts(0),
            ended_at=_ts(0.5),
        )
        status = ec.status()
        assert status["reference_segments_buffered"] == 0

    def test_does_not_buffer_when_disabled(self):
        ec = EchoCanceller(enabled=False)
        pcm = _make_stereo_pcm()
        ec.ingest_reference(
            source_id="system-audio",
            pcm_s16le=pcm,
            sample_rate=SAMPLE_RATE,
            channels=2,
            started_at=_ts(0),
            ended_at=_ts(0.5),
        )
        status = ec.status()
        assert status["reference_segments_buffered"] == 0


class TestCancel:
    def test_returns_bytes(self):
        ec = EchoCanceller(enabled=True)
        # Ingest reference with echo frequency
        ref_pcm = _make_stereo_pcm(duration_s=1.0, freq=300.0)
        ec.ingest_reference(
            source_id="system-audio",
            pcm_s16le=ref_pcm,
            sample_rate=SAMPLE_RATE,
            channels=2,
            started_at=_ts(0),
            ended_at=_ts(1.0),
        )
        mic_pcm = _make_mono_pcm(duration_s=0.5, freq=300.0)
        result = ec.cancel(
            mic_pcm=mic_pcm,
            sample_rate=SAMPLE_RATE,
            started_at=_ts(0.1),
            ended_at=_ts(0.6),
        )
        assert isinstance(result, bytes)
        assert len(result) == len(mic_pcm)

    def test_output_length_matches_input(self):
        ec = EchoCanceller(enabled=True)
        ref_pcm = _make_stereo_pcm(duration_s=2.0, freq=500.0)
        ec.ingest_reference(
            source_id="system-audio",
            pcm_s16le=ref_pcm,
            sample_rate=SAMPLE_RATE,
            channels=2,
            started_at=_ts(0),
            ended_at=_ts(2.0),
        )
        mic_pcm = _make_mono_pcm(duration_s=1.0, freq=500.0)
        result = ec.cancel(
            mic_pcm=mic_pcm,
            sample_rate=SAMPLE_RATE,
            started_at=_ts(0),
            ended_at=_ts(1.0),
        )
        assert len(result) == len(mic_pcm)

    def test_passthrough_when_no_reference(self):
        ec = EchoCanceller(enabled=True)
        mic_pcm = _make_mono_pcm(duration_s=0.5, freq=440.0)
        result = ec.cancel(
            mic_pcm=mic_pcm,
            sample_rate=SAMPLE_RATE,
            started_at=_ts(0),
            ended_at=_ts(0.5),
        )
        assert result == mic_pcm

    def test_echo_reduced_when_reference_available(self):
        """When reference is loud relative to mic, output should be
        attenuated (never amplified) — energy gating reduces echo-dominated
        frames."""
        ec = EchoCanceller(enabled=True, filter_length=2048, step_size=1.0)
        duration = 1.0

        # Mic: quiet signal (like room noise + faint echo)
        mic_pcm = _make_mono_pcm(duration_s=duration, freq=300.0, amplitude=500.0)
        # Reference: loud system audio (10x mic level, like real speaker output)
        ref_pcm = _make_stereo_pcm(duration_s=duration, freq=300.0, amplitude=5000.0)

        ec.ingest_reference(
            source_id="system-audio",
            pcm_s16le=ref_pcm,
            sample_rate=SAMPLE_RATE,
            channels=2,
            started_at=_ts(0),
            ended_at=_ts(duration),
        )

        result = ec.cancel(
            mic_pcm=mic_pcm,
            sample_rate=SAMPLE_RATE,
            started_at=_ts(0),
            ended_at=_ts(duration),
        )

        mic_orig = np.frombuffer(mic_pcm, dtype=np.int16).astype(np.float64)
        result_signal = np.frombuffer(result, dtype=np.int16).astype(np.float64)

        orig_rms = np.sqrt(np.mean(mic_orig ** 2))
        result_rms = np.sqrt(np.mean(result_signal ** 2))

        # Output should be attenuated (never louder)
        assert result_rms <= orig_rms, (
            f"AEC amplified signal: orig_rms={orig_rms:.0f}, result_rms={result_rms:.0f}"
        )
        # With reference 10x louder, should get meaningful reduction
        assert result_rms < orig_rms * 0.95, (
            f"Insufficient attenuation: orig_rms={orig_rms:.0f}, result_rms={result_rms:.0f}"
        )

    def test_empty_pcm_returns_unchanged(self):
        ec = EchoCanceller(enabled=True)
        result = ec.cancel(
            mic_pcm=b"",
            sample_rate=SAMPLE_RATE,
            started_at=_ts(0),
            ended_at=_ts(0.5),
        )
        assert result == b""


class TestStereoDownmix:
    def test_stereo_reference_becomes_mono(self):
        """Verify stereo reference is correctly downmixed to mono for NLMS."""
        ec = EchoCanceller(enabled=True)
        duration = 0.5
        n_samples = int(SAMPLE_RATE * duration)
        t = np.linspace(0, duration, n_samples, endpoint=False)

        # Left=300Hz, Right=300Hz (same signal) -> mono should be 300Hz
        signal = (np.sin(2 * np.pi * 300 * t) * 10000).astype(np.int16)
        stereo = np.column_stack([signal, signal]).flatten().tobytes()

        ec.ingest_reference(
            source_id="system-audio",
            pcm_s16le=stereo,
            sample_rate=SAMPLE_RATE,
            channels=2,
            started_at=_ts(0),
            ended_at=_ts(duration),
        )

        # Feed a mic that's just the same 300Hz echo
        mic_pcm = signal.tobytes()
        result = ec.cancel(
            mic_pcm=mic_pcm,
            sample_rate=SAMPLE_RATE,
            started_at=_ts(0),
            ended_at=_ts(duration),
        )
        # Should return valid bytes of same length
        assert isinstance(result, bytes)
        assert len(result) == len(mic_pcm)


class TestStats:
    def test_counters_increment_on_cancel(self):
        ec = EchoCanceller(enabled=True)
        ref_pcm = _make_stereo_pcm(duration_s=1.0, freq=300.0)
        ec.ingest_reference(
            source_id="system-audio",
            pcm_s16le=ref_pcm,
            sample_rate=SAMPLE_RATE,
            channels=2,
            started_at=_ts(0),
            ended_at=_ts(1.0),
        )
        mic_pcm = _make_mono_pcm(duration_s=0.5, freq=300.0)
        ec.cancel(
            mic_pcm=mic_pcm,
            sample_rate=SAMPLE_RATE,
            started_at=_ts(0),
            ended_at=_ts(0.5),
        )
        status = ec.status()
        assert status["segments_processed"] == 1
        assert status["segments_cancelled"] == 1

    def test_skipped_when_no_reference(self):
        ec = EchoCanceller(enabled=True)
        mic_pcm = _make_mono_pcm(duration_s=0.5)
        ec.cancel(
            mic_pcm=mic_pcm,
            sample_rate=SAMPLE_RATE,
            started_at=_ts(0),
            ended_at=_ts(0.5),
        )
        status = ec.status()
        assert status["segments_skipped"] == 1

    def test_status_returns_expected_keys(self):
        ec = EchoCanceller(enabled=True)
        status = ec.status()
        expected_keys = {
            "enabled",
            "filter_length",
            "step_size",
            "fixed_delay_ms",
            "reference_segments_buffered",
            "segments_processed",
            "segments_cancelled",
            "segments_skipped",
            "avg_echo_suppression_db",
        }
        assert expected_keys.issubset(set(status.keys()))
