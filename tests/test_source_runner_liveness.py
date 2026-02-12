from __future__ import annotations

from datetime import UTC, datetime, timedelta

from audio_assist.sources import FFmpegSourceRunner, MicrophoneSourceRunner


def _on_segment(_segment) -> None:  # noqa: ANN001
    return None


class _DummyStream:
    def __init__(self, active: bool = True) -> None:
        self.active = active


class _DummyProcess:
    def __init__(self, exit_code: int | None = None) -> None:
        self._exit_code = exit_code

    def poll(self) -> int | None:
        return self._exit_code


def test_microphone_runner_reports_not_running_when_stalled() -> None:
    runner = MicrophoneSourceRunner(
        source_id="system-audio",
        session_id="s1",
        on_segment=_on_segment,
        sample_rate=16000,
        channels=1,
        segment_seconds=4.5,
    )
    runner._running = True
    runner._stream = _DummyStream(active=True)
    runner._last_data_at = datetime.now(tz=UTC) - timedelta(seconds=runner._stall_timeout_seconds + 2.0)

    assert runner.is_running() is False


def test_microphone_runner_reports_running_with_recent_data() -> None:
    runner = MicrophoneSourceRunner(
        source_id="desk-mic",
        session_id="s2",
        on_segment=_on_segment,
        sample_rate=16000,
        channels=1,
        segment_seconds=4.5,
    )
    runner._running = True
    runner._stream = _DummyStream(active=True)
    runner._last_data_at = datetime.now(tz=UTC)

    assert runner.is_running() is True


def test_ffmpeg_runner_reports_not_running_when_stalled() -> None:
    runner = FFmpegSourceRunner(
        source_id="system-audio",
        session_id="s3",
        on_segment=_on_segment,
        sample_rate=16000,
        channels=1,
        segment_seconds=4.5,
        ffmpeg_input=":0",
    )
    runner._running = True
    runner._process = _DummyProcess(exit_code=None)
    runner._last_data_at = datetime.now(tz=UTC) - timedelta(seconds=runner._stall_timeout_seconds + 2.0)

    assert runner.is_running() is False

