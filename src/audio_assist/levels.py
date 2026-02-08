from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock

import numpy as np

from audio_assist.transcriber import AudioSegment


def _to_dbfs(value: float) -> float:
    if value <= 1e-9:
        return -96.0
    return float(max(-96.0, 20.0 * np.log10(value)))


@dataclass(slots=True)
class AudioLevelSnapshot:
    source_id: str
    level_dbfs: float
    peak_dbfs: float
    updated_at: datetime
    age_seconds: float
    silent: bool
    clipped: bool


@dataclass(slots=True)
class _AudioLevelState:
    level_dbfs: float
    peak_dbfs: float
    updated_at: datetime
    silent: bool
    clipped: bool
    segments_seen: int


class AudioLevelTracker:
    def __init__(
        self,
        silence_threshold_dbfs: float = -60.0,
        clip_threshold_normalized: float = 0.98,
    ):
        self._silence_threshold_dbfs = silence_threshold_dbfs
        self._clip_threshold_normalized = clip_threshold_normalized
        self._states: dict[str, _AudioLevelState] = {}
        self._lock = Lock()

    def ingest(self, segment: AudioSegment) -> None:
        samples = np.frombuffer(segment.pcm_s16le, dtype=np.int16)
        if samples.size == 0:
            return
        samples_f = samples.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples_f * samples_f)))
        peak = float(np.max(np.abs(samples_f)))
        level_dbfs = _to_dbfs(rms)
        peak_dbfs = _to_dbfs(peak)
        updated_at = segment.ended_at.astimezone(UTC)
        silent = level_dbfs <= self._silence_threshold_dbfs
        clipped = peak >= self._clip_threshold_normalized

        with self._lock:
            prev = self._states.get(segment.source_id)
            if prev is None:
                self._states[segment.source_id] = _AudioLevelState(
                    level_dbfs=level_dbfs,
                    peak_dbfs=peak_dbfs,
                    updated_at=updated_at,
                    silent=silent,
                    clipped=clipped,
                    segments_seen=1,
                )
                return

            # Smooth the level a bit so the UI meter is easier to read.
            smoothed_level = (0.6 * prev.level_dbfs) + (0.4 * level_dbfs)
            smoothed_silent = smoothed_level <= self._silence_threshold_dbfs
            self._states[segment.source_id] = _AudioLevelState(
                level_dbfs=smoothed_level,
                peak_dbfs=peak_dbfs,
                updated_at=updated_at,
                silent=smoothed_silent,
                clipped=clipped,
                segments_seen=prev.segments_seen + 1,
            )

    def snapshot(self) -> list[AudioLevelSnapshot]:
        now = datetime.now(tz=UTC)
        with self._lock:
            items = [
                AudioLevelSnapshot(
                    source_id=source_id,
                    level_dbfs=state.level_dbfs,
                    peak_dbfs=state.peak_dbfs,
                    updated_at=state.updated_at,
                    age_seconds=max(0.0, (now - state.updated_at).total_seconds()),
                    silent=state.silent,
                    clipped=state.clipped,
                )
                for source_id, state in self._states.items()
            ]
        return items
