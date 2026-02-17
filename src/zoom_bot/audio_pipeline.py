"""Audio pipeline: STT -> LLM -> TTS orchestrator.

Processes raw PCM audio, transcribes it via a pluggable STT provider,
generates an AI response via a pluggable LLM, synthesizes speech via
a pluggable TTS provider, and returns PCM audio via callback.
"""

from __future__ import annotations

import asyncio
import logging
import random
import struct
import time
from collections import deque
from typing import Callable

from zoom_bot.llm_providers import LLMProvider
from zoom_bot.stt_providers import STTProvider
from zoom_bot.tts_providers import TTSProvider

logger = logging.getLogger(__name__)

# Audio specs
SDK_SAMPLE_RATE = 32000
STT_SAMPLE_RATE = 16000


def resample_32k_to_16k(pcm_32k: bytes) -> bytes:
    """Downsample 32kHz mono 16-bit PCM to 16kHz by taking every other sample."""
    samples = struct.unpack(f"<{len(pcm_32k) // 2}h", pcm_32k)
    downsampled = samples[::2]
    return struct.pack(f"<{len(downsampled)}h", *downsampled)


def resample_to_32k(pcm_data: bytes, source_rate: int) -> bytes:
    """Resample PCM to 32kHz via linear interpolation."""
    if source_rate == SDK_SAMPLE_RATE:
        return pcm_data

    # Ensure even length for 16-bit samples
    if len(pcm_data) % 2:
        pcm_data = pcm_data[:-1]
    samples = struct.unpack(f"<{len(pcm_data) // 2}h", pcm_data)
    ratio = SDK_SAMPLE_RATE / source_rate
    out_len = int(len(samples) * ratio)
    result = []
    for i in range(out_len):
        src_idx = i / ratio
        idx = int(src_idx)
        frac = src_idx - idx
        if idx + 1 < len(samples):
            val = samples[idx] * (1 - frac) + samples[idx + 1] * frac
        else:
            val = samples[idx] if idx < len(samples) else 0
        result.append(max(-32768, min(32767, int(val))))
    return struct.pack(f"<{len(result)}h", *result)


class AudioPipeline:
    """Manages the STT -> LLM -> TTS pipeline for a meeting session.

    Provider-pluggable: accepts any STTProvider, LLMProvider, TTSProvider.
    """

    def __init__(
        self,
        stt: STTProvider,
        llm: LLMProvider,
        tts: TTSProvider,
        system_prompt: str,
        on_tts_audio: Callable[[bytes], None] | None = None,
        debounce_seconds: float = 0.8,
        max_conversation_turns: int = 20,
        llm_max_tokens: int = 80,
        llm_temperature: float = 0.5,
    ):
        self._stt = stt
        self._llm = llm
        self._tts = tts
        self._system_prompt = system_prompt
        self._on_tts_audio = on_tts_audio
        self._on_event: Callable[[str, dict], None] | None = None
        self._debounce_seconds = debounce_seconds
        self._max_turns = max_conversation_turns
        self._llm_max_tokens = llm_max_tokens
        self._llm_temperature = llm_temperature

        self._conversation: list[dict] = [
            {"role": "system", "content": system_prompt}
        ]
        self._audio_buffer: deque[bytes] = deque(maxlen=500)  # ~10s of 20ms chunks
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._is_speaking = False
        self._last_transcript_time = 0.0
        self._pending_text: list[str] = []
        self._debounce_task: asyncio.Task | None = None

        # Pre-cached filler audio (PCM 32kHz) for instant acknowledgment
        self._filler_cache: list[bytes] = []
        self._filler_phrases = [
            "Hmm.",
            "Oh.",
            "Hmm, let me think.",
            "Ooh, interesting.",
            "Oh yeah.",
            "Hmm, okay.",
            "Huh.",
            "Right.",
        ]

    def set_event_callback(self, cb: Callable[[str, dict], None]) -> None:
        """Set a callback for pipeline events (transcript, llm_response, tts, etc.)."""
        self._on_event = cb

    def _emit(self, event_type: str, data: dict) -> None:
        if self._on_event:
            try:
                self._on_event(event_type, {"type": event_type, "ts": time.time(), **data})
            except Exception:
                pass

    @property
    def conversation(self) -> list[dict]:
        """Return the current conversation history."""
        return list(self._conversation)

    async def start(self) -> None:
        """Start the pipeline (STT provider + audio sender loop)."""
        self._running = True

        # Wire up STT transcript callback
        self._stt.set_transcript_callback(self._on_transcript)
        await self._stt.start()

        self._tasks.append(asyncio.create_task(self._audio_sender_loop()))

        # Pre-cache filler audio in the background
        self._tasks.append(asyncio.create_task(self._precache_fillers()))

        logger.info("Audio pipeline started")

    async def _precache_fillers(self) -> None:
        """Pre-generate TTS for filler phrases so they can play instantly."""
        logger.info("Pre-caching %d filler phrases...", len(self._filler_phrases))
        for phrase in self._filler_phrases:
            try:
                result = await self._tts.synthesize(phrase)
                if result:
                    pcm, rate = result
                    pcm_32k = resample_to_32k(pcm, rate)
                    self._filler_cache.append(pcm_32k)
            except Exception:
                logger.debug("Failed to cache filler: %s", phrase)
        logger.info("Cached %d/%d filler clips", len(self._filler_cache), len(self._filler_phrases))

    async def stop(self) -> None:
        """Stop the pipeline and clean up."""
        self._running = False

        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

        await self._stt.stop()

        # Close providers that have a close method
        for provider in (self._llm, self._tts):
            close = getattr(provider, "close", None)
            if close:
                try:
                    await close()
                except Exception:
                    pass

        logger.info("Audio pipeline stopped")

    def feed_audio(self, pcm_32k: bytes) -> None:
        """Feed raw 32kHz PCM audio from the meeting into the pipeline."""
        if not self._running:
            return
        self._audio_buffer.append(pcm_32k)

    async def _audio_sender_loop(self) -> None:
        """Send buffered audio to the STT provider."""
        while self._running:
            if self._audio_buffer:
                chunk = self._audio_buffer.popleft()
                pcm_16k = resample_32k_to_16k(chunk)
                await self._stt.send_audio(pcm_16k)
            else:
                await asyncio.sleep(0.01)

    def _on_transcript(self, transcript: str) -> None:
        """Called by STT provider when a final transcript is ready."""
        self._last_transcript_time = time.time()

        # Don't process if we're currently speaking (avoid echo)
        if self._is_speaking:
            logger.debug("Ignoring transcript while speaking")
            return

        # Accumulate transcript segments and debounce
        self._pending_text.append(transcript)

        # Cancel any pending debounce timer
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()

        self._debounce_task = asyncio.create_task(self._debounced_respond())

    async def _debounced_respond(self) -> None:
        """Wait for silence, then combine pending text and respond."""
        await asyncio.sleep(self._debounce_seconds)

        combined = " ".join(self._pending_text).strip()
        self._pending_text.clear()

        if not combined:
            return

        logger.info("Combined transcript: %s", combined)
        self._emit("transcript", {"text": combined})

        self._conversation.append({"role": "user", "content": combined})

        # Keep conversation history manageable
        if len(self._conversation) > self._max_turns + 1:
            self._conversation = [self._conversation[0]] + self._conversation[-self._max_turns:]

        await self._respond()

    def _play_filler(self) -> None:
        """Play a random pre-cached filler clip instantly."""
        if not self._filler_cache or not self._on_tts_audio:
            return
        clip = random.choice(self._filler_cache)
        logger.info("Playing filler clip (%d bytes)", len(clip))
        self._on_tts_audio(clip)

    async def _respond(self) -> None:
        """Generate LLM response and synthesize speech."""
        try:
            self._is_speaking = True
            self._emit("responding", {"stage": "start"})

            # Play a filler immediately so the user hears something fast
            self._play_filler()

            # Step 1: LLM (runs while filler is playing)
            t0 = time.time()
            try:
                llm_response = await asyncio.wait_for(
                    self._llm.chat(
                        self._conversation,
                        max_tokens=self._llm_max_tokens,
                        temperature=self._llm_temperature,
                    ),
                    timeout=60.0,
                )
            except asyncio.TimeoutError:
                logger.error("LLM timed out after 60s")
                self._emit("error", {"stage": "llm", "error": "timeout"})
                return
            llm_elapsed = time.time() - t0

            if not llm_response:
                logger.warning("LLM returned empty response")
                self._emit("error", {"stage": "llm", "error": "empty response"})
                return

            logger.info("LLM response (%.1fs): %s", llm_elapsed, llm_response[:100])
            self._emit("llm_response", {"text": llm_response, "elapsed_s": round(llm_elapsed, 1)})
            self._conversation.append({"role": "assistant", "content": llm_response})

            # Step 2: TTS
            t0 = time.time()
            try:
                tts_result = await asyncio.wait_for(
                    self._tts.synthesize(llm_response),
                    timeout=90.0,
                )
            except asyncio.TimeoutError:
                logger.error("TTS timed out after 90s")
                self._emit("error", {"stage": "tts", "error": "timeout"})
                return
            tts_elapsed = time.time() - t0

            if not tts_result:
                logger.warning("TTS returned no audio")
                self._emit("error", {"stage": "tts", "error": "no audio"})
                return

            tts_audio, sample_rate = tts_result

            # Step 3: Send TTS audio to meeting (queues after filler).
            if self._on_tts_audio:
                pcm_32k = resample_to_32k(tts_audio, sample_rate)
                duration_s = len(pcm_32k) / (32000 * 2)
                logger.info("Sending TTS audio: %d bytes (%.1fs at 32kHz, synth %.1fs)",
                            len(pcm_32k), duration_s, tts_elapsed)
                self._emit("tts_audio", {
                    "bytes": len(pcm_32k), "duration_s": round(duration_s, 1),
                    "text": llm_response[:200], "synth_s": round(tts_elapsed, 1),
                })
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._on_tts_audio, pcm_32k)

        except Exception:
            logger.exception("Error in response pipeline")
            self._emit("error", {"stage": "pipeline", "error": "exception"})
        finally:
            self._is_speaking = False

    async def send_tts(self, text: str) -> None:
        """Synthesize and send speech directly, bypassing the LLM."""
        try:
            self._is_speaking = True
            logger.info("Sending direct TTS: %s", text)

            tts_result = await self._tts.synthesize(text)
            if tts_result and self._on_tts_audio:
                tts_audio, sample_rate = tts_result
                pcm_32k = resample_to_32k(tts_audio, sample_rate)
                logger.info("Direct TTS audio: %d bytes (%.1fs at 32kHz)",
                            len(pcm_32k), len(pcm_32k) / (32000 * 2))
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._on_tts_audio, pcm_32k)

            self._conversation.append({"role": "assistant", "content": text})
        except Exception:
            logger.exception("Error in direct TTS")
        finally:
            self._is_speaking = False
