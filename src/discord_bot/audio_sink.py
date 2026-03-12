"""Audio sink for receiving Discord voice data and feeding the pipeline.

Receives per-user PCM from py-cord's VoiceClient recording,
resamples to 32kHz mono, and feeds AudioPipeline.feed_audio().

Also maintains rolling buffers for audio recording / debugging.
"""

from __future__ import annotations

import logging
import re
import struct
import time
import wave
from collections import deque
from pathlib import Path

import discord

logger = logging.getLogger(__name__)

try:
    from pipeline_core import resample_48k_stereo_to_32k_mono
    logger.info("Using Rust-accelerated resample_48k_stereo_to_32k_mono")
except ImportError:
    logger.warning("pipeline_core not available, using Python fallback for resampling")

    def resample_48k_stereo_to_32k_mono(pcm_48k_stereo: bytes) -> bytes:
        """Resample 48kHz stereo s16le to 32kHz mono via decimation + mixing.

        Steps:
        1. Mix stereo to mono (average L+R channels)
        2. Resample 48kHz → 32kHz via linear interpolation (2:3 ratio)
        """
        # 48kHz stereo: each sample frame = 4 bytes (2 channels * 2 bytes)
        n_frames = len(pcm_48k_stereo) // 4
        if n_frames == 0:
            return b""

        # Unpack stereo interleaved (L0, R0, L1, R1, ...)
        all_samples = struct.unpack(f"<{n_frames * 2}h", pcm_48k_stereo[:n_frames * 4])

        # Mix to mono
        mono_48k = []
        for i in range(n_frames):
            left = all_samples[i * 2]
            right = all_samples[i * 2 + 1]
            mono_48k.append((left + right) // 2)

        # Resample 48kHz → 32kHz
        ratio = 32000 / 48000  # 0.6667
        out_len = int(len(mono_48k) * ratio)
        result = []
        for i in range(out_len):
            src_idx = i / ratio
            idx = int(src_idx)
            frac = src_idx - idx
            if idx + 1 < len(mono_48k):
                val = mono_48k[idx] * (1 - frac) + mono_48k[idx + 1] * frac
            else:
                val = mono_48k[min(idx, len(mono_48k) - 1)]
            result.append(max(-32768, min(32767, int(val))))
        return struct.pack(f"<{len(result)}h", *result)


# Recording constants
RECORDINGS_DIR = Path("data/recordings")
MAX_RECORDINGS = 50
# Rolling buffer: ~60s of audio at 48kHz stereo, 20ms chunks = 3000 chunks
# Each 20ms chunk at 48kHz stereo s16le = 48000 * 2 * 2 * 0.02 = 3840 bytes
BUFFER_MAXLEN = 3000  # ~60s


class PipelineSink(discord.sinks.Sink):
    """Discord audio sink that feeds received audio into AudioPipeline.

    Filters out the bot's own audio (echo rejection via user_id).
    Maintains rolling buffers of raw and resampled audio for recording.
    """

    def __init__(self, pipeline, bot_user_id: int):
        super().__init__()
        self._pipeline = pipeline
        self._bot_user_id = bot_user_id
        self._active_speakers: dict[int, float] = {}

        # Rolling audio buffers for recording
        self._raw_48k_buffer: deque[bytes] = deque(maxlen=BUFFER_MAXLEN)
        self._resampled_32k_buffer: deque[bytes] = deque(maxlen=BUFFER_MAXLEN)

        # Create recordings directory
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

    def write(self, data: bytes, user_id: int) -> None:
        """Receive per-user PCM audio from Discord.

        Args:
            data: Raw PCM audio (48kHz stereo s16le from Discord).
            user_id: Discord user ID of the speaker.
        """
        # Echo rejection: skip bot's own audio
        if user_id == self._bot_user_id:
            return

        # Track active speakers
        self._active_speakers[user_id] = time.monotonic()

        # Store raw 48kHz stereo in rolling buffer
        self._raw_48k_buffer.append(data)

        # Resample and feed to pipeline
        pcm_32k_mono = resample_48k_stereo_to_32k_mono(data)
        if pcm_32k_mono:
            # Store resampled 32kHz mono in rolling buffer
            self._resampled_32k_buffer.append(pcm_32k_mono)
            self._pipeline.feed_audio(pcm_32k_mono)

    def save_utterance(self, transcript: str = "", duration_s: float = 5.0) -> dict:
        """Save the last `duration_s` seconds of audio as WAV files.

        Saves both the raw 48kHz stereo and the resampled 32kHz mono.

        Args:
            transcript: The STT transcript text (used in filename).
            duration_s: How many seconds of audio to save (from the end of the buffer).

        Returns:
            dict with keys: timestamp, transcript, path_48k, path_32k, duration_s
        """
        now = time.time()
        ts_str = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))

        # Build a safe filename snippet from the transcript
        snippet = self._sanitize_filename(transcript)

        # Calculate how many chunks we need for the requested duration
        # 48kHz: 20ms chunks, so 50 chunks/s
        chunks_needed_48k = int(duration_s * 50)
        chunks_needed_32k = int(duration_s * 50)  # same chunk rate, different samples per chunk

        # Extract chunks from the end of the buffers
        raw_chunks = list(self._raw_48k_buffer)[-chunks_needed_48k:]
        mono_chunks = list(self._resampled_32k_buffer)[-chunks_needed_32k:]

        raw_pcm = b"".join(raw_chunks)
        mono_pcm = b"".join(mono_chunks)

        if not raw_pcm and not mono_pcm:
            logger.warning("No audio data in buffers to save")
            return {"error": "no audio data", "timestamp": ts_str}

        # Build filenames
        base = f"{ts_str}_{snippet}" if snippet else ts_str
        path_48k = RECORDINGS_DIR / f"{base}.48k.wav"
        path_32k = RECORDINGS_DIR / f"{base}.32k.wav"

        # Write 48kHz stereo WAV
        result: dict = {
            "timestamp": ts_str,
            "transcript": transcript,
            "duration_s": duration_s,
        }

        if raw_pcm:
            self._write_wav(path_48k, raw_pcm, sample_rate=48000, channels=2)
            result["path_48k"] = str(path_48k)
            actual_dur = len(raw_pcm) / (48000 * 2 * 2)
            result["actual_duration_48k"] = round(actual_dur, 2)

        if mono_pcm:
            self._write_wav(path_32k, mono_pcm, sample_rate=32000, channels=1)
            result["path_32k"] = str(path_32k)
            actual_dur = len(mono_pcm) / (32000 * 2)
            result["actual_duration_32k"] = round(actual_dur, 2)

        # Write metadata JSON alongside
        metadata_path = RECORDINGS_DIR / f"{base}.json"
        import json
        metadata_path.write_text(json.dumps(result, indent=2))

        logger.info(
            "Saved utterance: %s (48k=%d bytes, 32k=%d bytes)",
            base, len(raw_pcm), len(mono_pcm),
        )

        # Enforce max recordings limit
        self._cleanup_old_recordings()

        return result

    @staticmethod
    def _sanitize_filename(text: str, max_len: int = 40) -> str:
        """Create a safe filename snippet from transcript text."""
        if not text:
            return ""
        # Lowercase, replace non-alphanumeric with underscore
        safe = re.sub(r"[^a-z0-9]+", "_", text.lower().strip())
        # Trim leading/trailing underscores and truncate
        safe = safe.strip("_")[:max_len].rstrip("_")
        return safe

    @staticmethod
    def _write_wav(path: Path, pcm_data: bytes, sample_rate: int, channels: int) -> None:
        """Write raw PCM data as a WAV file."""
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)  # 16-bit / s16le
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_data)

    @staticmethod
    def _cleanup_old_recordings() -> None:
        """Keep only the most recent MAX_RECORDINGS sets of files."""
        if not RECORDINGS_DIR.exists():
            return

        # Group files by base name (without extension)
        wav_files = sorted(RECORDINGS_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        # Count unique recording sets (each set has .48k.wav and .32k.wav)
        bases = []
        seen = set()
        for f in wav_files:
            # Strip .48k.wav / .32k.wav to get base
            base = f.name
            for suffix in (".48k.wav", ".32k.wav"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            if base not in seen:
                seen.add(base)
                bases.append(base)

        # If over limit, delete oldest
        if len(bases) > MAX_RECORDINGS:
            to_remove = bases[: len(bases) - MAX_RECORDINGS]
            for base in to_remove:
                for pattern in (f"{base}.48k.wav", f"{base}.32k.wav", f"{base}.json"):
                    p = RECORDINGS_DIR / pattern
                    if p.exists():
                        try:
                            p.unlink()
                            logger.debug("Deleted old recording: %s", p.name)
                        except OSError:
                            pass

    @property
    def active_speakers(self) -> dict[int, float]:
        """Return dict of {user_id: last_active_time}."""
        return dict(self._active_speakers)

    def cleanup(self) -> None:
        self._active_speakers.clear()
        self._raw_48k_buffer.clear()
        self._resampled_32k_buffer.clear()
