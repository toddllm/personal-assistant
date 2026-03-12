from __future__ import annotations

import base64
from functools import lru_cache

import numpy as np

EMBEDDING_DIM = 192
_BAND_COUNT = 48


def decode_pcm_s16le_base64(payload: str, channels: int = 1) -> np.ndarray:
    try:
        pcm = base64.b64decode(payload, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Invalid pcm_s16le_base64 payload.") from exc
    return pcm_bytes_to_mono_samples(pcm, channels=channels)


def pcm_bytes_to_mono_samples(pcm: bytes, channels: int = 1) -> np.ndarray:
    if not pcm:
        return np.empty(0, dtype=np.float32)
    raw = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    use_channels = max(1, int(channels))
    if use_channels > 1:
        usable = raw.size - (raw.size % use_channels)
        if usable <= 0:
            return np.empty(0, dtype=np.float32)
        raw = raw[:usable].reshape(-1, use_channels).mean(axis=1)
    return raw / 32768.0


class LightweightSpeakerEmbeddingExtractor:
    """CPU-friendly 192-dim timbre embedding for local speaker matching."""

    def __init__(self, min_voice_dbfs: float = -46.0):
        self._min_voice_dbfs = float(min_voice_dbfs)

    def extract(self, samples: np.ndarray, sample_rate: int) -> np.ndarray | None:
        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        if audio.size == 0 or sample_rate <= 0:
            return None

        frame_size = max(256, int(sample_rate * 0.025))
        hop_size = max(128, int(sample_rate * 0.01))
        if audio.size < frame_size:
            return None

        n_fft = 1
        while n_fft < frame_size:
            n_fft *= 2

        window = np.hanning(frame_size).astype(np.float32)
        bands = _band_ranges(sample_rate, n_fft)
        feature_rows: list[np.ndarray] = []

        for start in range(0, audio.size - frame_size + 1, hop_size):
            frame = audio[start : start + frame_size]
            rms = float(np.sqrt(np.mean(frame * frame)))
            dbfs = -96.0 if rms <= 1e-9 else float(20.0 * np.log10(rms))
            if dbfs < self._min_voice_dbfs:
                continue

            emphasized = frame.copy()
            emphasized[1:] = emphasized[1:] - (0.97 * frame[:-1])
            spectrum = np.abs(np.fft.rfft(emphasized * window, n=n_fft)).astype(np.float32)
            power = np.square(spectrum[1:])
            if power.size == 0:
                continue

            band_values = np.empty(_BAND_COUNT, dtype=np.float32)
            for index, (start_idx, end_idx) in enumerate(bands):
                chunk = power[start_idx:end_idx]
                if chunk.size == 0:
                    band_values[index] = 0.0
                else:
                    band_values[index] = float(np.log1p(np.mean(chunk)))
            feature_rows.append(band_values)

        if len(feature_rows) < 4:
            return None

        matrix = np.stack(feature_rows, axis=0)
        mean = matrix.mean(axis=0)
        std = matrix.std(axis=0)
        q10 = np.quantile(matrix, 0.10, axis=0).astype(np.float32)
        q90 = np.quantile(matrix, 0.90, axis=0).astype(np.float32)
        embedding = np.concatenate([mean, std, q10, q90]).astype(np.float32)
        return _normalize(embedding)


@lru_cache(maxsize=32)
def _band_ranges(sample_rate: int, n_fft: int) -> tuple[tuple[int, int], ...]:
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / float(sample_rate))[1:]
    if freqs.size == 0:
        return tuple((0, 1) for _ in range(_BAND_COUNT))

    low_hz = max(60.0, float(freqs[0]))
    high_hz = max(low_hz * 1.2, min(float(sample_rate) * 0.5 * 0.98, 7600.0))
    edges = np.geomspace(low_hz, high_hz, _BAND_COUNT + 1)
    bands: list[tuple[int, int]] = []
    for index in range(_BAND_COUNT):
        start_idx = int(np.searchsorted(freqs, edges[index], side="left"))
        end_idx = int(np.searchsorted(freqs, edges[index + 1], side="right"))
        end_idx = min(freqs.size, max(start_idx + 1, end_idx))
        start_idx = min(start_idx, end_idx - 1)
        bands.append((start_idx, end_idx))
    return tuple(bands)


def _normalize(vector: np.ndarray) -> np.ndarray:
    centered = vector.astype(np.float32) - float(np.mean(vector))
    norm = float(np.linalg.norm(centered))
    if norm <= 1e-8:
        return np.zeros_like(centered)
    return centered / norm
