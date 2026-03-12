#!/usr/bin/env python3
"""Local voice chat — talk to the AI pipeline through your Mac mic and speakers.

Uses the same STT -> LLM -> TTS pipeline as the Zoom/Discord bots,
but captures from the local microphone and plays back through speakers.

Usage:
    python scripts/voice-chat.py

Environment variables (all optional, shown with defaults):
    STT_MODEL=base.en          Whisper model for STT
    LLM_MODEL=llama3.1:8b      Ollama model
    LLM_URL=http://127.0.0.1:11434
    TTS_URL=http://toddllm:8790
    TTS_VOICE=Vivian
    MIC_DEVICE="MacBook Pro Microphone"
    SPEAKER_DEVICE="MacBook Pro Speakers"
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading

import numpy as np
import sounddevice as sd

# Add src/ to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zoom_bot.audio_pipeline import AudioPipeline
from zoom_bot.llm_providers import OpenAICompatibleLLM
from zoom_bot.stt_providers import FasterWhisperBatchSTT
from zoom_bot.tts_providers import QwenTTS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("voice-chat")

# Config from env
STT_MODEL = os.environ.get("STT_MODEL", "base.en")
LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:11434")
LLM_MODEL = os.environ.get("LLM_MODEL", "llama3.1:8b")
TTS_URL = os.environ.get("TTS_URL", "http://toddllm:8790")
TTS_VOICE = os.environ.get("TTS_VOICE", "Vivian")
MIC_DEVICE = os.environ.get("MIC_DEVICE", "MacBook Pro Microphone")
SPEAKER_DEVICE = os.environ.get("SPEAKER_DEVICE", "MacBook Pro Speakers")
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", (
    "You are a friendly AI voice assistant. "
    "Keep every response to ONE short sentence (under 15 words). "
    "Be conversational and natural."
))

# Audio constants
CAPTURE_RATE = 48000
PIPELINE_RATE = 32000
PLAYBACK_RATE = 48000


def resample_48k_to_32k_mono(pcm_48k_mono_f32: np.ndarray) -> bytes:
    """Resample float32 48kHz mono to 32kHz s16le bytes."""
    ratio = PIPELINE_RATE / CAPTURE_RATE  # 0.6667
    out_len = int(len(pcm_48k_mono_f32) * ratio)
    indices = np.arange(out_len) / ratio
    idx = indices.astype(int)
    frac = indices - idx
    safe_idx = np.minimum(idx + 1, len(pcm_48k_mono_f32) - 1)
    resampled = pcm_48k_mono_f32[idx] * (1 - frac) + pcm_48k_mono_f32[safe_idx] * frac
    pcm_s16 = np.clip(resampled * 32767, -32768, 32767).astype(np.int16)
    return pcm_s16.tobytes()


def resample_32k_to_48k(pcm_32k: bytes) -> np.ndarray:
    """Resample 32kHz s16le mono to 48kHz float32 for playback."""
    n_samples = len(pcm_32k) // 2
    if n_samples == 0:
        return np.array([], dtype=np.float32)
    samples = np.frombuffer(pcm_32k, dtype=np.int16).astype(np.float32) / 32768.0
    ratio = PLAYBACK_RATE / PIPELINE_RATE  # 1.5
    out_len = int(n_samples * ratio)
    indices = np.arange(out_len) / ratio
    idx = indices.astype(int)
    frac = indices - idx
    safe_idx = np.minimum(idx + 1, n_samples - 1)
    resampled = samples[idx] * (1 - frac) + samples[safe_idx] * frac
    return resampled.astype(np.float32)


def find_device_index(name: str, kind: str) -> int | str:
    """Find device index by name substring."""
    for i, d in enumerate(sd.query_devices()):
        if kind == "input" and d["max_input_channels"] == 0:
            continue
        if kind == "output" and d["max_output_channels"] == 0:
            continue
        if name.lower() in d["name"].lower():
            return i
    return name


class AudioPlayer:
    """Plays TTS audio and signals the mic to mute during playback (echo suppression)."""

    def __init__(self, device: str | int, sample_rate: int = PLAYBACK_RATE):
        self._device = device
        self._sample_rate = sample_rate
        self.is_playing = threading.Event()  # set while TTS audio is playing

    def push_audio(self, pcm_32k: bytes) -> None:
        """Resample 32kHz to 48kHz and play through speakers (blocking)."""
        audio_48k = resample_32k_to_48k(pcm_32k)
        if len(audio_48k) == 0:
            return

        duration = len(audio_48k) / self._sample_rate
        logger.info("\n>>> AI speaking (%.1fs) ...", duration)

        self.is_playing.set()
        try:
            sd.play(audio_48k, samplerate=self._sample_rate, device=self._device, blocking=True)
        except Exception:
            logger.exception("Playback error")
        finally:
            self.is_playing.clear()
            logger.info(">>> Done speaking. Listening...\n")


async def main():
    print()
    print("=" * 60)
    print("  Local Voice Chat (fully local AI)")
    print("=" * 60)
    print(f"  STT:     faster-whisper ({STT_MODEL})")
    print(f"  LLM:     {LLM_MODEL} @ {LLM_URL}")
    print(f"  TTS:     Qwen3 ({TTS_VOICE}) @ {TTS_URL}")
    print(f"  Mic:     {MIC_DEVICE}")
    print(f"  Speaker: {SPEAKER_DEVICE}")
    print("=" * 60)
    print()
    print("  HOW TO USE:")
    print("  1. Speak into your MacBook microphone")
    print("  2. Wait ~1 second after you stop talking")
    print("  3. The AI will respond through your speakers")
    print("  4. Press Ctrl+C to quit")
    print()
    print("=" * 60)
    print()

    # Create providers
    stt = FasterWhisperBatchSTT(model=STT_MODEL, silence_ms=800)
    llm = OpenAICompatibleLLM(
        base_url=LLM_URL,
        model=LLM_MODEL,
        api_key="",
        timeout=30.0,
    )
    tts = QwenTTS(url=TTS_URL, voice=TTS_VOICE)

    # Audio player with echo suppression signal
    speaker_idx = find_device_index(SPEAKER_DEVICE, "output")
    player = AudioPlayer(device=speaker_idx)

    # Create pipeline
    pipeline = AudioPipeline(
        stt=stt,
        llm=llm,
        tts=tts,
        system_prompt=SYSTEM_PROMPT,
        on_tts_audio=player.push_audio,
        debounce_seconds=0.8,
        llm_max_tokens=80,
        llm_temperature=0.5,
    )

    print("Loading Whisper model...")
    await pipeline.start()
    print("Ready! Start talking.\n")

    # Start microphone capture
    mic_idx = find_device_index(MIC_DEVICE, "input")
    stop_event = threading.Event()

    def audio_callback(indata, frames, time_info, status):
        if status:
            logger.debug("Mic status: %s", status)
        # Echo suppression: don't feed mic audio while TTS is playing
        if player.is_playing.is_set():
            return
        mono = indata[:, 0] if indata.shape[1] > 1 else indata.flatten()
        pcm_32k = resample_48k_to_32k_mono(mono)
        pipeline.feed_audio(pcm_32k)

    try:
        with sd.InputStream(
            device=mic_idx,
            samplerate=CAPTURE_RATE,
            channels=1,
            dtype="float32",
            blocksize=4800,  # 100ms at 48kHz
            callback=audio_callback,
        ):
            while not stop_event.is_set():
                await asyncio.sleep(0.1)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        await pipeline.stop()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
