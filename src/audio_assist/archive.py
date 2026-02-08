from __future__ import annotations

from datetime import UTC, datetime
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
        path = self.segment_path(
            source_id=segment.source_id,
            started_at=segment.started_at,
            ended_at=segment.ended_at,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(self._channels)
                wav.setsampwidth(2)  # PCM16
                wav.setframerate(segment.sample_rate)
                wav.writeframes(segment.pcm_s16le)
        return path

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    @staticmethod
    def segment_filename(started_at: datetime, ended_at: datetime) -> str:
        started = started_at.astimezone(UTC)
        ended = ended_at.astimezone(UTC)
        return (
            f"{started.strftime('%H%M%S_%f')}"
            f"__{ended.strftime('%H%M%S_%f')}.wav"
        )

    def segment_path(self, source_id: str, started_at: datetime, ended_at: datetime) -> Path:
        started = started_at.astimezone(UTC)
        day_dir = self._root_dir / source_id / started.strftime("%Y-%m-%d")
        return day_dir / self.segment_filename(started, ended_at)

    def load_segment(self, source_id: str, started_at: datetime, ended_at: datetime) -> tuple[bytes, int] | None:
        path = self.segment_path(source_id=source_id, started_at=started_at, ended_at=ended_at)
        if not path.exists():
            return None
        try:
            with wave.open(str(path), "rb") as wav:
                channels = int(wav.getnchannels())
                sample_width = int(wav.getsampwidth())
                frame_rate = int(wav.getframerate())
                frames = int(wav.getnframes())
                raw = wav.readframes(frames)
        except Exception:
            return None
        if channels != self._channels or sample_width != 2 or frame_rate <= 0:
            return None
        return raw, frame_rate
