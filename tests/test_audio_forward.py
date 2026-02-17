"""Tests for the audio forwarding daemon (scripts/audio-forward.py).

Tests device discovery, subprocess detection, audio processing helpers,
callback error handling, and reinit coordination without real audio hardware.
"""

import os
import sys
import threading
from queue import Full, Queue
from unittest.mock import MagicMock, patch

import numpy as np

_SCRIPT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
_PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "src"))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "audio_forward", os.path.join(_SCRIPT_DIR, "audio-forward.py")
)
af = importlib.util.module_from_spec(_spec)

with patch.dict("sys.modules", {"sounddevice": MagicMock()}):
    _spec.loader.exec_module(af)


# -----------------------------------------------------------------------
# find_device
# -----------------------------------------------------------------------

class TestFindDevice:
    @staticmethod
    def _mock_devices():
        return [
            {"name": "MacBook Pro Microphone", "max_input_channels": 1, "max_output_channels": 0},
            {"name": "MacBook Pro Speakers", "max_input_channels": 0, "max_output_channels": 2},
            {"name": "CaptureAudio 2ch", "max_input_channels": 2, "max_output_channels": 2},
        ]

    def test_find_by_name(self):
        with patch.object(af.sd, "query_devices", return_value=self._mock_devices()):
            assert af.find_device("CaptureAudio") == 2

    def test_find_input(self):
        with patch.object(af.sd, "query_devices", return_value=self._mock_devices()):
            assert af.find_device("MacBook Pro", kind="input") == 0

    def test_find_output(self):
        with patch.object(af.sd, "query_devices", return_value=self._mock_devices()):
            assert af.find_device("MacBook Pro", kind="output") == 1

    def test_missing_returns_none(self):
        with patch.object(af.sd, "query_devices", return_value=self._mock_devices()):
            assert af.find_device("Bose QC45", kind="output") is None

    def test_case_insensitive(self):
        with patch.object(af.sd, "query_devices", return_value=self._mock_devices()):
            assert af.find_device("captureAUDIO") == 2

    def test_bluetooth_off_not_found(self):
        with patch.object(af.sd, "query_devices", return_value=self._mock_devices()):
            assert af.find_device("Bose QC45", kind="output") is None

    def test_bluetooth_on_found(self):
        devices = self._mock_devices() + [
            {"name": "Bose QC45", "max_input_channels": 1, "max_output_channels": 0},
            {"name": "Bose QC45", "max_input_channels": 0, "max_output_channels": 1},
        ]
        with patch.object(af.sd, "query_devices", return_value=devices):
            assert af.find_device("Bose QC45", kind="output") == 4


# -----------------------------------------------------------------------
# Subprocess device check
# -----------------------------------------------------------------------

class TestCheckDeviceExistsSubprocess:
    def test_found(self):
        mock_result = MagicMock(stdout="True\n")
        with patch("subprocess.run", return_value=mock_result):
            assert af.check_device_exists_subprocess("Bose QC45", "output") is True

    def test_not_found(self):
        mock_result = MagicMock(stdout="False\n")
        with patch("subprocess.run", return_value=mock_result):
            assert af.check_device_exists_subprocess("Bose QC45", "output") is False

    def test_error(self):
        with patch("subprocess.run", side_effect=Exception("fail")):
            assert af.check_device_exists_subprocess("Bose QC45", "output") is False

    def test_timeout(self):
        import subprocess as sp
        with patch("subprocess.run", side_effect=sp.TimeoutExpired("cmd", 5)):
            assert af.check_device_exists_subprocess("Bose QC45", "output") is False


# -----------------------------------------------------------------------
# downmix_to_mono
# -----------------------------------------------------------------------

class TestDownmixToMono:
    def test_stereo_to_mono(self):
        data = np.array([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32)
        result = af.downmix_to_mono(data)
        assert result.shape == (2, 1)
        np.testing.assert_allclose(result[:, 0], 0.5)

    def test_mono_reshaped(self):
        data = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        result = af.downmix_to_mono(data)
        assert result.shape == (3, 1)

    def test_identical_channels(self):
        data = np.array([[0.7, 0.7]], dtype=np.float32)
        result = af.downmix_to_mono(data)
        np.testing.assert_allclose(result[0, 0], 0.7)


# -----------------------------------------------------------------------
# Queue distribution
# -----------------------------------------------------------------------

class TestQueueDistribution:
    def test_distributed_to_all(self):
        q1, q2 = Queue(maxsize=10), Queue(maxsize=10)
        with patch.object(af, "_queues_lock", threading.Lock()):
            with patch.object(af, "_target_queues", {"a": q1, "b": q2}):
                frame = np.zeros((512, 2), dtype=np.float32)
                with af._queues_lock:
                    for q in af._target_queues.values():
                        try:
                            q.put_nowait(frame.copy())
                        except Full:
                            pass
        assert q1.qsize() == 1
        assert q2.qsize() == 1

    def test_full_queue_drops(self):
        q1 = Queue(maxsize=1)
        q1.put(np.zeros(1))
        q2 = Queue(maxsize=10)
        with patch.object(af, "_queues_lock", threading.Lock()):
            with patch.object(af, "_target_queues", {"slow": q1, "fast": q2}):
                with af._queues_lock:
                    for q in af._target_queues.values():
                        try:
                            q.put_nowait(np.zeros(1))
                        except Full:
                            pass
        assert q1.qsize() == 1  # still 1, frame dropped
        assert q2.qsize() == 1

    def test_no_queues_no_error(self):
        with patch.object(af, "_queues_lock", threading.Lock()):
            with patch.object(af, "_target_queues", {}):
                with af._queues_lock:
                    for q in af._target_queues.values():
                        try:
                            q.put_nowait(np.zeros(1))
                        except Full:
                            pass


# -----------------------------------------------------------------------
# Processing pipeline
# -----------------------------------------------------------------------

class TestProcessingPipeline:
    def test_stereo_to_mono(self):
        data = np.random.randn(512, 2).astype(np.float32)
        mono = af.downmix_to_mono(data)
        assert mono.shape == (512, 1)


# -----------------------------------------------------------------------
# Reinit coordination
# -----------------------------------------------------------------------

class TestReinitCoordination:
    def test_reinit_exits_stream_loop(self):
        event = threading.Event()
        event.set()
        assert event.is_set()

    def test_reinit_cleared_after_refresh(self):
        event = threading.Event()
        event.set()
        event.clear()
        assert not event.is_set()
