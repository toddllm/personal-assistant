from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread
from typing import Callable
from uuid import uuid4

from audio_assist.transcriber import AudioSegment


OnSegment = Callable[[AudioSegment], None]


@dataclass(slots=True)
class SourceRuntime:
    source_id: str
    session_id: str
    source_type: str
    runner: "BaseSourceRunner"
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


class BaseSourceRunner:
    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def is_running(self) -> bool:
        raise NotImplementedError


class MicrophoneSourceRunner(BaseSourceRunner):
    def __init__(
        self,
        source_id: str,
        session_id: str,
        on_segment: OnSegment,
        sample_rate: int,
        channels: int,
        segment_seconds: float,
        overlap_seconds: float = 0.0,
        device: str | int | None = None,
    ):
        self._source_id = source_id
        self._session_id = session_id
        self._on_segment = on_segment
        self._sample_rate = sample_rate
        self._channels = channels
        self._segment_seconds = max(0.5, float(segment_seconds))
        overlap_value = max(0.0, float(overlap_seconds))
        if overlap_value >= self._segment_seconds:
            overlap_value = max(0.0, self._segment_seconds - 0.2)
        self._overlap_seconds = overlap_value
        self._segment_step_seconds = self._segment_seconds - self._overlap_seconds
        if self._segment_step_seconds <= 0:
            self._segment_step_seconds = self._segment_seconds
            self._overlap_seconds = 0.0
        self._device = device
        self._stream = None
        self._running = False
        self._lock = Lock()
        self._buffer = bytearray()
        self._segment_bytes = int(sample_rate * self._segment_seconds * channels * 2)
        self._segment_step_bytes = min(
            self._segment_bytes,
            max(2 * channels, int(sample_rate * self._segment_step_seconds * channels * 2)),
        )
        self._stream_started_at: datetime | None = None
        self._segments_emitted = 0
        self._last_data_at: datetime | None = None
        self._stall_timeout_seconds = max(15.0, self._segment_seconds * 6.0)

    def start(self) -> None:
        if self._running:
            return
        try:
            import sounddevice as sd
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("sounddevice is required for microphone capture.") from exc

        def callback(indata, frames, time_info, status):  # noqa: ANN001
            del frames, time_info
            if status:
                # Keep stream alive; status warnings are transient on some devices.
                pass
            with self._lock:
                self._buffer.extend(bytes(indata))
                self._last_data_at = datetime.now(tz=UTC)
                self._drain_segments()

        self._stream = sd.RawInputStream(
            samplerate=self._sample_rate,
            channels=self._channels,
            dtype="int16",
            callback=callback,
            device=self._device,
            blocksize=int(self._sample_rate * 0.25),
        )
        self._stream.start()
        now = datetime.now(tz=UTC)
        self._stream_started_at = now
        self._last_data_at = now
        self._segments_emitted = 0
        self._running = True

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._last_data_at = None

    def is_running(self) -> bool:
        if not self._running:
            return False
        if self._stream is None:
            return False
        try:
            if hasattr(self._stream, "active") and not bool(self._stream.active):  # type: ignore[attr-defined]
                return False
        except Exception:
            pass
        if self._last_data_at is not None:
            age_seconds = max(0.0, (datetime.now(tz=UTC) - self._last_data_at).total_seconds())
            if age_seconds > self._stall_timeout_seconds:
                return False
        return True

    def _drain_segments(self) -> None:
        if self._stream_started_at is None:
            return
        while len(self._buffer) >= self._segment_bytes:
            chunk = bytes(self._buffer[: self._segment_bytes])
            del self._buffer[: self._segment_step_bytes]

            started_at = self._stream_started_at + timedelta(
                seconds=self._segments_emitted * self._segment_step_seconds
            )
            ended_at = started_at + timedelta(seconds=self._segment_seconds)
            self._segments_emitted += 1

            try:
                self._on_segment(
                    AudioSegment(
                        source_id=self._source_id,
                        session_id=self._session_id,
                        started_at=started_at,
                        ended_at=ended_at,
                        pcm_s16le=chunk,
                        sample_rate=self._sample_rate,
                    )
                )
            except Exception:  # noqa: BLE001
                # Do not crash the audio callback thread on transient queue issues.
                pass


class FFmpegSourceRunner(BaseSourceRunner):
    def __init__(
        self,
        source_id: str,
        session_id: str,
        on_segment: OnSegment,
        sample_rate: int,
        channels: int,
        segment_seconds: float,
        ffmpeg_input: str,
        overlap_seconds: float = 0.0,
        ffmpeg_input_format: str | None = None,
        ffmpeg_extra_args: list[str] | None = None,
    ):
        self._source_id = source_id
        self._session_id = session_id
        self._on_segment = on_segment
        self._sample_rate = sample_rate
        self._channels = channels
        self._segment_seconds = max(0.5, float(segment_seconds))
        overlap_value = max(0.0, float(overlap_seconds))
        if overlap_value >= self._segment_seconds:
            overlap_value = max(0.0, self._segment_seconds - 0.2)
        self._overlap_seconds = overlap_value
        self._segment_step_seconds = self._segment_seconds - self._overlap_seconds
        if self._segment_step_seconds <= 0:
            self._segment_step_seconds = self._segment_seconds
            self._overlap_seconds = 0.0
        self._ffmpeg_input = ffmpeg_input
        self._ffmpeg_input_format = ffmpeg_input_format
        self._ffmpeg_extra_args = ffmpeg_extra_args or []
        self._segment_bytes = int(sample_rate * self._segment_seconds * channels * 2)
        self._segment_step_bytes = min(
            self._segment_bytes,
            max(2 * channels, int(sample_rate * self._segment_step_seconds * channels * 2)),
        )
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: Thread | None = None
        self._stop_event = Event()
        self._running = False
        self._last_data_at: datetime | None = None
        self._stall_timeout_seconds = max(15.0, self._segment_seconds * 6.0)

    def start(self) -> None:
        if self._running:
            return
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg is required for ffmpeg source capture.")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if self._ffmpeg_input_format:
            cmd += ["-f", self._ffmpeg_input_format]
        cmd += ["-i", self._ffmpeg_input]
        cmd += self._ffmpeg_extra_args
        cmd += [
            "-ac",
            str(self._channels),
            "-ar",
            str(self._sample_rate),
            "-f",
            "s16le",
            "-",
        ]
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._stop_event.clear()
        self._last_data_at = datetime.now(tz=UTC)
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()
        self._running = True

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
        self._process = None
        self._last_data_at = None

    def is_running(self) -> bool:
        if not self._running:
            return False
        if self._process is not None and self._process.poll() is not None:
            return False
        if self._last_data_at is not None:
            age_seconds = max(0.0, (datetime.now(tz=UTC) - self._last_data_at).total_seconds())
            if age_seconds > self._stall_timeout_seconds:
                return False
        return True

    def _run(self) -> None:
        if self._process is None or self._process.stdout is None:
            return
        start = datetime.now(tz=UTC)
        segment_index = 0
        buffer = bytearray()
        while not self._stop_event.is_set():
            chunk = self._process.stdout.read(4096)
            if not chunk:
                break
            self._last_data_at = datetime.now(tz=UTC)
            buffer.extend(chunk)
            while len(buffer) >= self._segment_bytes:
                seg = bytes(buffer[: self._segment_bytes])
                del buffer[: self._segment_step_bytes]
                started_at = start + timedelta(seconds=segment_index * self._segment_step_seconds)
                ended_at = started_at + timedelta(seconds=self._segment_seconds)
                segment_index += 1
                try:
                    self._on_segment(
                        AudioSegment(
                            source_id=self._source_id,
                            session_id=self._session_id,
                            started_at=started_at,
                            ended_at=ended_at,
                            pcm_s16le=seg,
                            sample_rate=self._sample_rate,
                        )
                    )
                except Exception:  # noqa: BLE001
                    pass
        self._running = False


class SourceManager:
    def __init__(
        self,
        on_segment: OnSegment,
        sample_rate: int,
        channels: int,
        segment_seconds: float,
        segment_overlap_seconds: float = 0.0,
    ):
        self._on_segment = on_segment
        self._sample_rate = sample_rate
        self._channels = channels
        self._segment_seconds = segment_seconds
        self._segment_overlap_seconds = segment_overlap_seconds
        self._sources: dict[str, SourceRuntime] = {}
        self._lock = Lock()

    def start_mic(
        self,
        source_id: str | None = None,
        device: str | int | None = None,
        channels: int | None = None,
    ) -> SourceRuntime:
        source_id = source_id or f"mic-{uuid4().hex[:8]}"
        session_id = f"{source_id}-{uuid4().hex[:12]}"
        runner_channels = self._channels if channels is None else max(1, int(channels))
        runner = MicrophoneSourceRunner(
            source_id=source_id,
            session_id=session_id,
            on_segment=self._on_segment,
            sample_rate=self._sample_rate,
            channels=runner_channels,
            segment_seconds=self._segment_seconds,
            overlap_seconds=self._segment_overlap_seconds,
            device=device,
        )
        runtime = SourceRuntime(source_id=source_id, session_id=session_id, source_type="mic", runner=runner)
        with self._lock:
            if source_id in self._sources and self._sources[source_id].runner.is_running():
                raise ValueError(f"Source '{source_id}' is already running.")
            runner.start()
            self._sources[source_id] = runtime
        return runtime

    def start_ffmpeg(
        self,
        ffmpeg_input: str,
        source_id: str | None = None,
        ffmpeg_input_format: str | None = None,
        ffmpeg_extra_args: list[str] | None = None,
    ) -> SourceRuntime:
        source_id = source_id or f"ffmpeg-{uuid4().hex[:8]}"
        session_id = f"{source_id}-{uuid4().hex[:12]}"
        runner = FFmpegSourceRunner(
            source_id=source_id,
            session_id=session_id,
            on_segment=self._on_segment,
            sample_rate=self._sample_rate,
            channels=self._channels,
            segment_seconds=self._segment_seconds,
            overlap_seconds=self._segment_overlap_seconds,
            ffmpeg_input=ffmpeg_input,
            ffmpeg_input_format=ffmpeg_input_format,
            ffmpeg_extra_args=ffmpeg_extra_args,
        )
        runtime = SourceRuntime(
            source_id=source_id,
            session_id=session_id,
            source_type="ffmpeg",
            runner=runner,
        )
        with self._lock:
            if source_id in self._sources and self._sources[source_id].runner.is_running():
                raise ValueError(f"Source '{source_id}' is already running.")
            runner.start()
            self._sources[source_id] = runtime
        return runtime

    def stop(self, source_id: str) -> bool:
        with self._lock:
            runtime = self._sources.get(source_id)
            if runtime is None:
                return False
            runtime.runner.stop()
            return True

    def statuses(self) -> list[SourceRuntime]:
        with self._lock:
            return list(self._sources.values())

    def get(self, source_id: str) -> SourceRuntime | None:
        with self._lock:
            return self._sources.get(source_id)

    def stop_all(self) -> None:
        with self._lock:
            for runtime in self._sources.values():
                runtime.runner.stop()
