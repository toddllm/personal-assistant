"""Baseline noise suppression for mic audio segments."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import Lock

import numpy as np

logger = logging.getLogger(__name__)

_HAS_NOISEREDUCE = False
try:
    import noisereduce as nr

    _HAS_NOISEREDUCE = True
except ImportError:
    pass


@dataclass(slots=True)
class SuppressionStats:
    segments_processed: int = 0
    segments_suppressed: int = 0
    segments_skipped: int = 0
    avg_snr_improvement_db: float = 0.0
    total_snr_improvement_db: float = 0.0


class NoiseSuppressor:
    """Apply noise suppression to PCM audio before transcription.

    Uses noisereduce (spectral gating) for mic audio. System-audio is
    passed through without modification since remote voices are already
    clean from the sender's processing.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        mic_source_prefix: str = "desk-mic",
        prop_decrease: float = 0.85,
        stationary: bool = True,
    ):
        self._enabled = enabled and _HAS_NOISEREDUCE
        self._mic_source_prefix = mic_source_prefix
        self._prop_decrease = max(0.0, min(1.0, float(prop_decrease)))
        self._stationary = bool(stationary)
        self._lock = Lock()
        self._stats = SuppressionStats()

    @property
    def available(self) -> bool:
        return _HAS_NOISEREDUCE

    @property
    def enabled(self) -> bool:
        return self._enabled

    def should_suppress(self, source_id: str) -> bool:
        """Only apply noise suppression to mic sources, not system-audio."""
        if not self._enabled:
            return False
        # Apply to mic sources only
        lowered = source_id.lower()
        return (
            lowered.startswith(self._mic_source_prefix)
            or "mic" in lowered
            or "microphone" in lowered
        )

    def suppress(
        self,
        pcm_s16le: bytes,
        sample_rate: int,
        source_id: str,
    ) -> bytes:
        """Apply noise suppression to PCM audio. Returns processed bytes."""
        if not self.should_suppress(source_id):
            with self._lock:
                self._stats.segments_skipped += 1
            return pcm_s16le

        try:
            audio = np.frombuffer(pcm_s16le, dtype=np.int16).astype(np.float32) / 32768.0
            if audio.size == 0:
                return pcm_s16le

            # Compute pre-suppression RMS
            pre_rms = float(np.sqrt(np.mean(audio * audio)))
            if pre_rms <= 1e-8:
                with self._lock:
                    self._stats.segments_skipped += 1
                return pcm_s16le

            # Apply noisereduce spectral gating
            cleaned = nr.reduce_noise(
                y=audio,
                sr=sample_rate,
                prop_decrease=self._prop_decrease,
                stationary=self._stationary,
                n_fft=512,
                hop_length=128,
            )

            # Compute post-suppression RMS for quality tracking
            post_rms = float(np.sqrt(np.mean(cleaned * cleaned)))
            if post_rms > 1e-8 and pre_rms > 1e-8:
                # SNR improvement = reduction in noise floor
                snr_improvement = max(0.0, 20.0 * np.log10(pre_rms / max(1e-9, post_rms)))
            else:
                snr_improvement = 0.0

            # Convert back to PCM s16le
            result = np.clip(cleaned * 32768.0, -32768, 32767).astype(np.int16).tobytes()

            with self._lock:
                self._stats.segments_processed += 1
                self._stats.segments_suppressed += 1
                self._stats.total_snr_improvement_db += snr_improvement
                if self._stats.segments_suppressed > 0:
                    self._stats.avg_snr_improvement_db = (
                        self._stats.total_snr_improvement_db / self._stats.segments_suppressed
                    )

            return result

        except Exception:  # noqa: BLE001
            logger.debug("Noise suppression failed for source=%s, passing through.", source_id)
            with self._lock:
                self._stats.segments_skipped += 1
            return pcm_s16le

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "available": self.available,
                "prop_decrease": self._prop_decrease,
                "stationary": self._stationary,
                "segments_processed": self._stats.segments_processed,
                "segments_suppressed": self._stats.segments_suppressed,
                "segments_skipped": self._stats.segments_skipped,
                "avg_snr_improvement_db": round(self._stats.avg_snr_improvement_db, 2),
            }
