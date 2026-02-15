"""Acoustic echo cancellation using STFT spectral gating."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from threading import Lock

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ReferenceSegment:
    mono_f64: np.ndarray
    sample_rate: int
    started_at: datetime
    ended_at: datetime


@dataclass(slots=True)
class _AECStats:
    segments_processed: int = 0
    segments_cancelled: int = 0
    segments_skipped: int = 0
    avg_echo_suppression_db: float = 0.0
    total_echo_suppression_db: float = 0.0


class EchoCanceller:
    """Apply acoustic echo cancellation to mic audio.

    System-audio segments are buffered as reference. When a mic segment
    arrives, frequencies where the reference has energy are attenuated
    in the mic signal via STFT spectral gating.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        mic_source_prefix: str = "desk-mic",
        filter_length: int = 2048,
        step_size: float = 0.1,
        fixed_delay_ms: float = 0.0,
        reference_buffer_seconds: float = 15.0,
    ):
        self._enabled = enabled
        self._mic_source_prefix = mic_source_prefix
        self._filter_length = max(64, int(filter_length))
        self._step_size = max(0.001, min(2.0, float(step_size)))
        self._fixed_delay_ms = float(fixed_delay_ms)
        self._reference_buffer_seconds = max(1.0, float(reference_buffer_seconds))
        self._lock = Lock()
        self._stats = _AECStats()
        maxlen = max(2, int(self._reference_buffer_seconds / 4.0) + 1)
        self._reference_buffer: deque[_ReferenceSegment] = deque(maxlen=maxlen)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def should_cancel(self, source_id: str) -> bool:
        """Return True only for desk-mic sources when AEC is enabled."""
        if not self._enabled:
            return False
        lowered = source_id.lower()
        return lowered.startswith(self._mic_source_prefix)

    def _is_system_audio(self, source_id: str) -> bool:
        lowered = source_id.lower()
        return lowered.startswith("system-audio") or "blackhole" in lowered

    def ingest_reference(
        self,
        *,
        source_id: str,
        pcm_s16le: bytes,
        sample_rate: int,
        channels: int,
        started_at: datetime,
        ended_at: datetime,
    ) -> None:
        """Buffer a system-audio segment as AEC reference."""
        if not self._enabled:
            return
        if not self._is_system_audio(source_id):
            return

        raw = np.frombuffer(pcm_s16le, dtype=np.int16).astype(np.float64)
        if raw.size == 0:
            return

        # Downmix stereo (interleaved) to mono by averaging L+R
        if channels >= 2:
            usable = raw.size - (raw.size % channels)
            if usable == 0:
                return
            raw = raw[:usable].reshape(-1, channels).mean(axis=1)

        with self._lock:
            self._reference_buffer.append(
                _ReferenceSegment(
                    mono_f64=raw,
                    sample_rate=sample_rate,
                    started_at=started_at,
                    ended_at=ended_at,
                )
            )

    def _find_overlapping_reference(
        self,
        started_at: datetime,
        ended_at: datetime,
        target_samples: int,
        sample_rate: int,
    ) -> np.ndarray | None:
        """Find and assemble reference audio that overlaps the mic segment."""
        with self._lock:
            refs = list(self._reference_buffer)

        if not refs:
            return None

        delay_s = self._fixed_delay_ms / 1000.0

        collected: list[np.ndarray] = []
        for ref in refs:
            if ref.ended_at <= started_at or ref.started_at >= ended_at:
                continue

            audio = ref.mono_f64
            if ref.sample_rate != sample_rate:
                ratio = sample_rate / ref.sample_rate
                new_len = int(len(audio) * ratio)
                indices = np.linspace(0, len(audio) - 1, new_len)
                audio = np.interp(indices, np.arange(len(audio)), audio)

            if delay_s > 0:
                delay_samples = int(delay_s * sample_rate)
                if 0 < delay_samples < len(audio):
                    audio = audio[delay_samples:]

            collected.append(audio)

        if not collected:
            return None

        reference = np.concatenate(collected)

        if len(reference) >= target_samples:
            reference = reference[:target_samples]
        else:
            reference = np.pad(reference, (0, target_samples - len(reference)))

        return reference

    def _spectral_gate(
        self,
        mic: np.ndarray,
        ref: np.ndarray,
    ) -> np.ndarray:
        """Echo cancellation via per-frame energy gating.

        For each short frame, computes the reference energy relative to
        the mic energy. When reference energy dominates (suggesting the
        mic is mostly hearing echo), the frame is attenuated. When the
        mic has energy well above the reference (user speaking), the
        frame passes through.

        The gain is applied directly in the time domain (multiply raw
        mic samples by a scalar <= 1.0), so no synthesis artifacts are
        possible. Smooth gain transitions via overlapping Hann windows.

        step_size controls maximum attenuation (1.0 = full, 0.5 = half).
        """
        n = len(mic)
        frame_len = min(self._filter_length, n)
        frame_len = frame_len - (frame_len % 2)
        if frame_len < 64:
            return mic.copy()
        hop = frame_len // 2
        eps = 1e-10
        max_attenuation = min(1.0, self._step_size)

        n_frames = max(1, (n - frame_len) // hop + 1)

        # Compute per-sample gain via windowed overlap-add
        gain_signal = np.zeros(n, dtype=np.float64)
        gain_norm = np.zeros(n, dtype=np.float64)
        window = np.hanning(frame_len)

        for i in range(n_frames):
            start = i * hop
            end = start + frame_len
            if end > n:
                break

            mic_frame = mic[start:end]
            ref_frame = ref[start:end]

            # Frame energies
            mic_energy = np.mean(mic_frame * mic_frame) + eps
            ref_energy = np.mean(ref_frame * ref_frame) + eps

            # Echo ratio: how much of the acoustic scene is reference audio.
            # When ref >> mic, echo dominates → attenuate.
            # When mic >> ref, voice dominates → pass through.
            # Scale ref_energy down to account for speaker→mic path loss.
            # The digital reference is much louder than its acoustic echo
            # at the mic. path_loss models this: 0.1 = -10dB (aggressive),
            # 0.01 = -20dB (conservative).
            path_loss = 0.1
            ref_adjusted = ref_energy * path_loss
            echo_ratio = ref_adjusted / (mic_energy + ref_adjusted)

            # Gain: 1.0 when no echo, (1 - max_attenuation) when all echo
            frame_gain = 1.0 - max_attenuation * echo_ratio

            # Apply windowed gain for smooth transitions
            gain_signal[start:end] += frame_gain * window
            gain_norm[start:end] += window

        # Normalize the overlap-add gain
        norm_mask = gain_norm > eps
        gain = np.ones(n, dtype=np.float64)
        gain[norm_mask] = gain_signal[norm_mask] / gain_norm[norm_mask]

        # Apply gain to original mic signal
        return mic * gain

    def cancel(
        self,
        mic_pcm: bytes,
        sample_rate: int,
        started_at: datetime,
        ended_at: datetime,
    ) -> bytes:
        """Apply AEC to mic PCM. Returns processed bytes.

        If no reference is available or an error occurs, returns
        the original PCM unchanged.
        """
        if not mic_pcm:
            return mic_pcm

        mic_signal = np.frombuffer(mic_pcm, dtype=np.int16).astype(np.float64)
        if mic_signal.size == 0:
            return mic_pcm

        n_samples = len(mic_signal)

        reference = self._find_overlapping_reference(
            started_at, ended_at, n_samples, sample_rate,
        )

        if reference is None:
            with self._lock:
                self._stats.segments_processed += 1
                self._stats.segments_skipped += 1
            return mic_pcm

        try:
            pre_rms = float(np.sqrt(np.mean(mic_signal * mic_signal)))
            if pre_rms <= 1e-8:
                with self._lock:
                    self._stats.segments_processed += 1
                    self._stats.segments_skipped += 1
                return mic_pcm

            output = self._spectral_gate(mic_signal, reference)

            post_rms = float(np.sqrt(np.mean(output * output)))
            if post_rms > 1e-8 and pre_rms > 1e-8:
                raw_suppression_db = 20.0 * np.log10(pre_rms / max(1e-9, post_rms))
                suppression_db = max(0.0, raw_suppression_db)
            else:
                raw_suppression_db = 0.0
                suppression_db = 0.0

            # Safety: if output is louder than input, discard and pass through
            if raw_suppression_db < -1.0:
                logger.debug(
                    "AEC safety fallback: raw=%.1fdB, passing through original.",
                    raw_suppression_db,
                )
                with self._lock:
                    self._stats.segments_processed += 1
                    self._stats.segments_skipped += 1
                return mic_pcm

            ref_rms = float(np.sqrt(np.mean(reference * reference)))
            logger.debug(
                "AEC: pre=%.0f post=%.0f ref=%.0f suppression=%.1fdB",
                pre_rms, post_rms, ref_rms, raw_suppression_db,
            )

            result = np.clip(output, -32768, 32767).astype(np.int16).tobytes()

            with self._lock:
                self._stats.segments_processed += 1
                self._stats.segments_cancelled += 1
                self._stats.total_echo_suppression_db += suppression_db
                if self._stats.segments_cancelled > 0:
                    self._stats.avg_echo_suppression_db = (
                        self._stats.total_echo_suppression_db
                        / self._stats.segments_cancelled
                    )

            return result

        except Exception:  # noqa: BLE001
            logger.debug("Echo cancellation failed, passing through original audio.")
            with self._lock:
                self._stats.segments_processed += 1
                self._stats.segments_skipped += 1
            return mic_pcm

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "filter_length": self._filter_length,
                "step_size": self._step_size,
                "fixed_delay_ms": self._fixed_delay_ms,
                "reference_segments_buffered": len(self._reference_buffer),
                "segments_processed": self._stats.segments_processed,
                "segments_cancelled": self._stats.segments_cancelled,
                "segments_skipped": self._stats.segments_skipped,
                "avg_echo_suppression_db": round(self._stats.avg_echo_suppression_db, 2),
            }
