"""TTS audio source for Discord voice playback.

Thread-safe queue-based AudioSource. Pipeline's on_tts_audio callback
pushes 48kHz mono PCM chunks. Discord's player thread pulls 20ms frames.
"""

from __future__ import annotations

import logging
import struct
import threading

import discord

logger = logging.getLogger(__name__)

# 20ms at 48kHz stereo s16le = 960 samples * 2 channels * 2 bytes = 3840 bytes
FRAME_SIZE = 3840
SILENCE_FRAME = b"\x00" * FRAME_SIZE

# Volume scaling (0.0 = silent, 1.0 = full)
TTS_VOLUME = 0.35

# Try Rust-accelerated imports
_use_rust_buffer = False
_use_rust_resample = False

try:
    from pipeline_core import AudioBuffer as _RustAudioBuffer
    _use_rust_buffer = True
    logger.info("Using Rust-accelerated AudioBuffer")
except ImportError:
    pass

try:
    from pipeline_core import resample_32k_to_48k_stereo as _rust_resample
    _use_rust_resample = True
    logger.info("Using Rust-accelerated resample_32k_to_48k_stereo")
except ImportError:
    pass


class TTSAudioSource(discord.AudioSource):
    """Queue-based PCM audio source for Discord voice playback."""

    def __init__(self):
        if _use_rust_buffer:
            self._rust_buffer = _RustAudioBuffer(sample_rate=48000, channels=2)
        else:
            self._buffer = bytearray()
            self._lock = threading.Lock()

    def read(self) -> bytes:
        """Return a 20ms frame (3840 bytes at 48kHz stereo s16le).

        Returns b"" when buffer is empty to signal end of playback.
        The bot will call play() again when new TTS audio arrives.
        """
        if _use_rust_buffer:
            frame = self._rust_buffer.read_frame()
            return frame if frame is not None else b""
        with self._lock:
            if len(self._buffer) >= FRAME_SIZE:
                frame = bytes(self._buffer[:FRAME_SIZE])
                del self._buffer[:FRAME_SIZE]
                return frame
            elif len(self._buffer) > 0:
                # Pad remaining audio with silence to fill a frame
                frame = bytes(self._buffer) + b"\x00" * (FRAME_SIZE - len(self._buffer))
                self._buffer.clear()
                return frame
        return b""

    def push_audio(self, pcm_48k_stereo: bytes) -> None:
        """Push PCM audio data into the playback buffer.

        Called from the pipeline's on_tts_audio callback (potentially
        from a different thread).
        """
        if _use_rust_buffer:
            self._rust_buffer.push(pcm_48k_stereo)
            return
        with self._lock:
            self._buffer.extend(pcm_48k_stereo)

    @property
    def has_audio(self) -> bool:
        if _use_rust_buffer:
            return self._rust_buffer.is_playing
        with self._lock:
            return len(self._buffer) >= FRAME_SIZE

    @property
    def duration_remaining_ms(self) -> float:
        """Estimated remaining audio duration in milliseconds."""
        if _use_rust_buffer:
            return self._rust_buffer.duration_remaining_ms()
        with self._lock:
            # 48kHz stereo 16-bit = 192000 bytes/sec = 192 bytes/ms
            return len(self._buffer) / 192.0

    def clear(self) -> None:
        """Clear the audio buffer (e.g. for cancel)."""
        if _use_rust_buffer:
            self._rust_buffer.clear()
            return
        with self._lock:
            self._buffer.clear()

    def is_opus(self) -> bool:
        return False  # py-cord handles Opus encoding

    def cleanup(self) -> None:
        self.clear()


def resample_32k_to_48k(pcm_32k: bytes) -> bytes:
    """Resample 32kHz s16le mono PCM to 48kHz stereo for Discord.

    Discord requires 48kHz stereo s16le (3840 bytes per 20ms frame).
    """
    if _use_rust_resample:
        return bytes(_rust_resample(pcm_32k, TTS_VOLUME))
    n_samples = len(pcm_32k) // 2
    if n_samples == 0:
        return b""
    samples = struct.unpack(f"<{n_samples}h", pcm_32k)
    ratio = 48000 / 32000  # 1.5
    out_len = int(n_samples * ratio)
    result = []
    for i in range(out_len):
        src_idx = i / ratio
        idx = int(src_idx)
        frac = src_idx - idx
        if idx + 1 < n_samples:
            val = samples[idx] * (1 - frac) + samples[idx + 1] * frac
        else:
            val = samples[min(idx, n_samples - 1)]
        s = max(-32768, min(32767, int(val * TTS_VOLUME)))
        # Stereo: duplicate to left and right channels
        result.append(s)
        result.append(s)
    return struct.pack(f"<{len(result)}h", *result)
