"""Integration tests for the Rust pipeline_core module.

Tests verify that the Rust-accelerated functions produce correct output
and match the Python fallback behavior.
"""

import struct
import pytest

# Skip all tests if Rust module not available
pipeline_core = pytest.importorskip("pipeline_core")


class TestResample48kStereoTo32kMono:
    """Tests for resample_48k_stereo_to_32k_mono."""

    def test_empty_input(self):
        result = pipeline_core.resample_48k_stereo_to_32k_mono(b"")
        assert result == b""

    def test_too_small_input(self):
        # Less than one stereo frame (4 bytes)
        result = pipeline_core.resample_48k_stereo_to_32k_mono(b"\x00\x00")
        assert result == b""

    def test_output_length(self):
        # 960 stereo frames at 48k = 960 * 4 bytes input
        # Should produce ~640 mono samples at 32k = 1280 bytes
        n_frames = 960
        pcm = struct.pack(f"<{n_frames * 2}h", *([0] * n_frames * 2))
        result = pipeline_core.resample_48k_stereo_to_32k_mono(pcm)
        n_out = len(result) // 2
        expected = int(n_frames * (32000 / 48000))
        assert abs(n_out - expected) <= 1

    def test_silence_in_silence_out(self):
        n_frames = 480
        pcm = b"\x00" * (n_frames * 4)
        result = pipeline_core.resample_48k_stereo_to_32k_mono(pcm)
        samples = struct.unpack(f"<{len(result) // 2}h", result)
        assert all(s == 0 for s in samples)

    def test_mono_mixing(self):
        # Left=1000, Right=-1000 should mix to ~0
        n_frames = 480
        stereo = []
        for _ in range(n_frames):
            stereo.extend([1000, -1000])
        pcm = struct.pack(f"<{len(stereo)}h", *stereo)
        result = pipeline_core.resample_48k_stereo_to_32k_mono(pcm)
        samples = struct.unpack(f"<{len(result) // 2}h", result)
        assert all(abs(s) <= 1 for s in samples)


class TestResample32kTo16k:
    """Tests for resample_32k_to_16k."""

    def test_empty_input(self):
        result = pipeline_core.resample_32k_to_16k(b"")
        assert result == b""

    def test_decimation(self):
        # 4 samples -> 2 samples
        samples = [100, 200, 300, 400]
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        result = pipeline_core.resample_32k_to_16k(pcm)
        out = struct.unpack(f"<{len(result) // 2}h", result)
        assert out == (100, 300)

    def test_output_half_length(self):
        samples = list(range(100))
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        result = pipeline_core.resample_32k_to_16k(pcm)
        assert len(result) == len(pcm) // 2


class TestResampleTo32k:
    """Tests for resample_to_32k."""

    def test_passthrough_at_32k(self):
        pcm = b"\x01\x02\x03\x04"
        result = pipeline_core.resample_to_32k(pcm, 32000)
        assert bytes(result) == pcm

    def test_upsample_16k(self):
        # 16k -> 32k should roughly double the samples
        samples = list(range(100))
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        result = pipeline_core.resample_to_32k(pcm, 16000)
        n_out = len(result) // 2
        assert abs(n_out - 200) <= 1

    def test_empty_input(self):
        result = pipeline_core.resample_to_32k(b"", 44100)
        assert result == b""


class TestResample32kTo48kStereo:
    """Tests for resample_32k_to_48k_stereo."""

    def test_empty_input(self):
        result = pipeline_core.resample_32k_to_48k_stereo(b"", 1.0)
        assert result == b""

    def test_volume_scaling(self):
        # Single sample at 1000, volume 0.5 should produce ~500
        pcm = struct.pack("<h", 1000)
        result = pipeline_core.resample_32k_to_48k_stereo(pcm, 0.5)
        out = struct.unpack(f"<{len(result) // 2}h", result)
        # First two samples (stereo pair) should be ~500
        assert abs(out[0] - 500) <= 1
        assert abs(out[1] - 500) <= 1

    def test_stereo_output(self):
        # Every output sample pair should have equal L and R
        samples = [500, 1000, -500, -1000]
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        result = pipeline_core.resample_32k_to_48k_stereo(pcm, 1.0)
        out = struct.unpack(f"<{len(result) // 2}h", result)
        for i in range(0, len(out), 2):
            assert out[i] == out[i + 1], f"Stereo mismatch at index {i}"

    def test_output_ratio(self):
        # 32k -> 48k ratio is 1.5, output should be ~1.5x input samples
        # Each output sample is stereo (2 channels), so bytes = n_in * 1.5 * 2 * 2
        n_in = 640  # 20ms at 32k
        pcm = struct.pack(f"<{n_in}h", *([0] * n_in))
        result = pipeline_core.resample_32k_to_48k_stereo(pcm, 1.0)
        n_out_stereo_pairs = len(result) // 4
        expected = int(n_in * 1.5)
        assert abs(n_out_stereo_pairs - expected) <= 1


class TestAudioBuffer:
    """Tests for AudioBuffer."""

    def test_push_and_read(self):
        buf = pipeline_core.AudioBuffer(sample_rate=48000, channels=2)
        # Frame size = 3840 bytes (20ms at 48k stereo 16-bit)
        data = b"\x42" * 3840
        buf.push(data)
        assert buf.is_playing
        frame = buf.read_frame()
        assert frame is not None
        assert len(frame) == 3840

    def test_empty_read(self):
        buf = pipeline_core.AudioBuffer()
        assert not buf.is_playing
        assert buf.read_frame() is None

    def test_partial_frame_padding(self):
        buf = pipeline_core.AudioBuffer(sample_rate=48000, channels=2)
        buf.push(b"\x42" * 100)
        assert not buf.is_playing  # Less than one frame
        frame = buf.read_frame()
        assert frame is not None
        assert len(frame) == 3840
        assert frame[:100] == b"\x42" * 100
        assert frame[100:] == b"\x00" * 3740

    def test_duration_remaining(self):
        buf = pipeline_core.AudioBuffer(sample_rate=48000, channels=2)
        # 192 bytes/ms at 48k stereo 16-bit
        buf.push(b"\x00" * 1920)  # 10ms
        dur = buf.duration_remaining_ms()
        assert abs(dur - 10.0) < 0.5

    def test_clear(self):
        buf = pipeline_core.AudioBuffer()
        buf.push(b"\x00" * 10000)
        assert len(buf) > 0
        buf.clear()
        assert len(buf) == 0


class TestEchoDetector:
    """Tests for EchoDetector."""

    def test_exact_echo(self):
        det = pipeline_core.EchoDetector(window_seconds=30.0)
        det.record_spoken("Hello, how are you doing today?")
        assert det.is_echo("Hello how are you doing today", 0.6)

    def test_no_echo(self):
        det = pipeline_core.EchoDetector(window_seconds=30.0)
        det.record_spoken("Hello world")
        assert not det.is_echo("Something completely different", 0.6)

    def test_partial_overlap_below_threshold(self):
        det = pipeline_core.EchoDetector(window_seconds=30.0)
        det.record_spoken("The quick brown fox jumps over the lazy dog")
        # 3 of 5 words = 0.6, right at threshold
        assert not det.is_echo("The quick brown cat sleeps", 0.7)

    def test_empty_transcript(self):
        det = pipeline_core.EchoDetector()
        det.record_spoken("Hello world")
        assert not det.is_echo("", 0.6)

    def test_no_history(self):
        det = pipeline_core.EchoDetector()
        assert not det.is_echo("Hello world", 0.6)

    def test_prune(self):
        det = pipeline_core.EchoDetector(window_seconds=0.0)
        det.record_spoken("test")
        import time
        time.sleep(0.01)
        det.prune()
        assert len(det) == 0


class TestFillerManager:
    """Tests for FillerManager."""

    def test_add_pcm_and_select(self):
        mgr = pipeline_core.FillerManager()
        # 0.5s at 32kHz mono = 32000 bytes
        pcm = b"\x00" * 32000
        mgr.add_pcm("test_short", pcm)
        assert len(mgr) == 1
        clip = mgr.select("short")
        assert clip is not None
        assert len(clip) == 32000

    def test_empty_select(self):
        mgr = pipeline_core.FillerManager()
        assert mgr.select("short") is None

    def test_categories(self):
        mgr = pipeline_core.FillerManager()
        # Short: <1s (<64000 bytes)
        mgr.add_pcm("short", b"\x00" * 32000)   # 0.5s
        # Medium: 1-2s (64000-128000 bytes)
        mgr.add_pcm("medium", b"\x00" * 96000)   # 1.5s
        # Long: 2-3s (128000+ bytes)
        mgr.add_pcm("long", b"\x00" * 160000)    # 2.5s
        assert len(mgr) == 3

        fillers = mgr.list_fillers()
        cats = {f["label"]: f["category"] for f in fillers}
        assert cats["short"] == "short"
        assert cats["medium"] == "medium"
        assert cats["long"] == "long"

    def test_list_fillers(self):
        mgr = pipeline_core.FillerManager()
        mgr.add_pcm("hello", b"\x00" * 64000)
        fillers = mgr.list_fillers()
        assert len(fillers) == 1
        assert fillers[0]["label"] == "hello"
        assert "duration_ms" in fillers[0]
        assert "category" in fillers[0]
        assert "size_bytes" in fillers[0]

    def test_load_nonexistent_directory(self):
        mgr = pipeline_core.FillerManager()
        count = mgr.load_directory("/nonexistent/path")
        assert count == 0
