from __future__ import annotations

from datetime import UTC
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING
import wave

if TYPE_CHECKING:
    from audio_assist.transcriber import AudioSegment


class AudioArchiver:
    def __init__(self, root_dir: Path, channels: int = 1):
        self._root_dir = root_dir
        self._channels = channels
        self._lock = Lock()
        self._root_dir.mkdir(parents=True, exist_ok=True)

    def save(self, segment: AudioSegment) -> Path:
        started = segment.started_at.astimezone(UTC)
        ended = segment.ended_at.astimezone(UTC)
        day_dir = self._root_dir / segment.source_id / started.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        filename = (
            f"{started.strftime('%H%M%S_%f')}"
            f"__{ended.strftime('%H%M%S_%f')}.wav"
        )
        path = day_dir / filename
        with self._lock:
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(self._channels)
                wav.setsampwidth(2)  # PCM16
                wav.setframerate(segment.sample_rate)
                wav.writeframes(segment.pcm_s16le)
        return path
