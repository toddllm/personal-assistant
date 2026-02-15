"""Audio pipeline: Deepgram STT -> Groq LLM -> ElevenLabs TTS.

Processes raw PCM audio from the Zoom SDK, transcribes it, generates
an AI response, synthesizes speech, and returns PCM audio to send
back into the meeting.
"""

from __future__ import annotations

import asyncio
import io
import logging
import struct
import time
from collections import deque
from typing import Callable

import httpx

logger = logging.getLogger(__name__)

# Deepgram WebSocket URL for real-time STT
DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"

# Groq API
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

# ElevenLabs API
ELEVENLABS_API_URL = "https://api.elevenlabs.io/v1/text-to-speech"
ELEVENLABS_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"  # Sarah

# Audio specs
SDK_SAMPLE_RATE = 32000
DEEPGRAM_SAMPLE_RATE = 16000


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
    """Manages the STT -> LLM -> TTS pipeline for a meeting session."""

    def __init__(
        self,
        deepgram_api_key: str,
        groq_api_key: str,
        elevenlabs_api_key: str,
        system_prompt: str,
        on_tts_audio: Callable[[bytes], None] | None = None,
    ):
        self._deepgram_key = deepgram_api_key
        self._groq_key = groq_api_key
        self._elevenlabs_key = elevenlabs_api_key
        self._system_prompt = system_prompt
        self._on_tts_audio = on_tts_audio

        self._conversation: list[dict] = [
            {"role": "system", "content": system_prompt}
        ]
        self._audio_buffer = deque(maxlen=500)  # ~10s of 20ms chunks
        self._running = False
        self._deepgram_ws = None
        self._tasks: list[asyncio.Task] = []
        self._http_client: httpx.AsyncClient | None = None
        self._is_speaking = False
        self._last_transcript_time = 0.0

    async def start(self) -> None:
        """Start the pipeline (Deepgram WebSocket + processing loop)."""
        self._running = True
        self._http_client = httpx.AsyncClient(timeout=30)
        self._tasks.append(asyncio.create_task(self._deepgram_loop()))
        self._tasks.append(asyncio.create_task(self._audio_sender_loop()))
        logger.info("Audio pipeline started")

    async def stop(self) -> None:
        """Stop the pipeline and clean up."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

        if self._deepgram_ws:
            try:
                await self._deepgram_ws.close()
            except Exception:
                pass

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        logger.info("Audio pipeline stopped")

    def feed_audio(self, pcm_32k: bytes) -> None:
        """Feed raw 32kHz PCM audio from the meeting into the pipeline."""
        if not self._running:
            return
        self._audio_buffer.append(pcm_32k)

    async def _audio_sender_loop(self) -> None:
        """Send buffered audio to Deepgram WebSocket."""
        while self._running:
            if self._deepgram_ws and self._audio_buffer:
                chunk = self._audio_buffer.popleft()
                pcm_16k = resample_32k_to_16k(chunk)
                try:
                    await self._deepgram_ws.send(pcm_16k)
                except Exception:
                    logger.debug("Failed to send audio to Deepgram")
            else:
                await asyncio.sleep(0.01)

    async def _deepgram_loop(self) -> None:
        """Maintain Deepgram WebSocket connection and process transcripts."""
        import websockets

        params = (
            f"?encoding=linear16&sample_rate={DEEPGRAM_SAMPLE_RATE}"
            f"&channels=1&model=nova-2&punctuate=true"
            f"&interim_results=false&endpointing=300"
            f"&vad_events=true&smart_format=true"
        )

        while self._running:
            try:
                headers = {"Authorization": f"Token {self._deepgram_key}"}
                async with websockets.connect(
                    DEEPGRAM_WS_URL + params,
                    additional_headers=headers,
                    ping_interval=20,
                ) as ws:
                    self._deepgram_ws = ws
                    logger.info("Deepgram WebSocket connected")

                    async for msg in ws:
                        if not self._running:
                            break
                        await self._handle_deepgram_message(msg)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Deepgram connection error, reconnecting...")
                self._deepgram_ws = None
                await asyncio.sleep(2)

    async def _handle_deepgram_message(self, msg: str | bytes) -> None:
        """Process a Deepgram transcript result."""
        import json

        if isinstance(msg, bytes):
            return

        try:
            data = json.loads(msg)
        except (json.JSONDecodeError, TypeError):
            return

        if data.get("type") != "Results":
            return

        channel = data.get("channel", {})
        alternatives = channel.get("alternatives", [])
        if not alternatives:
            return

        transcript = alternatives[0].get("transcript", "").strip()
        if not transcript:
            return

        is_final = data.get("is_final", False)
        if not is_final:
            return

        logger.info("Transcript: %s", transcript)
        self._last_transcript_time = time.time()

        # Don't process if we're currently speaking (avoid echo)
        if self._is_speaking:
            logger.debug("Ignoring transcript while speaking")
            return

        # Add to conversation and get LLM response
        self._conversation.append({"role": "user", "content": transcript})

        # Keep conversation history manageable (last 20 turns)
        if len(self._conversation) > 21:
            self._conversation = [self._conversation[0]] + self._conversation[-20:]

        asyncio.create_task(self._respond())

    async def _respond(self) -> None:
        """Generate LLM response and synthesize speech."""
        try:
            self._is_speaking = True

            # Step 1: Groq LLM
            llm_response = await self._call_groq()
            if not llm_response:
                return

            logger.info("LLM response: %s", llm_response[:100])
            self._conversation.append({"role": "assistant", "content": llm_response})

            # Step 2: ElevenLabs TTS
            tts_audio = await self._call_elevenlabs(llm_response)
            if not tts_audio:
                return

            # Step 3: Send TTS audio to meeting via WebSocket → WebRTC injection.
            # Resample to 32kHz (on_tts_audio resamples to 48kHz for Web Audio).
            # Run in thread executor to avoid blocking asyncio event loop.
            if self._on_tts_audio:
                pcm_32k = resample_to_32k(tts_audio, 22050)
                logger.info("Sending TTS audio: %d bytes (%.1fs at 32kHz)",
                            len(pcm_32k), len(pcm_32k) / (32000 * 2))
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._on_tts_audio, pcm_32k)

        except Exception:
            logger.exception("Error in response pipeline")
        finally:
            self._is_speaking = False

    async def _call_groq(self) -> str | None:
        """Call Groq LLM for a chat completion."""
        if not self._http_client:
            return None

        try:
            resp = await self._http_client.post(
                GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {self._groq_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROQ_MODEL,
                    "messages": self._conversation,
                    "temperature": 0.5,
                    "max_tokens": 200,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except Exception:
            logger.exception("Groq API error")
            return None

    async def _call_elevenlabs(self, text: str) -> bytes | None:
        """Call ElevenLabs TTS and return raw PCM audio."""
        if not self._http_client:
            return None

        try:
            # output_format MUST be a query parameter (not in JSON body),
            # otherwise ElevenLabs ignores it and returns MP3 by default.
            resp = await self._http_client.post(
                f"{ELEVENLABS_API_URL}/{ELEVENLABS_VOICE_ID}?output_format=pcm_22050",
                headers={
                    "xi-api-key": self._elevenlabs_key,
                    "Content-Type": "application/json",
                },
                json={
                    "text": text,
                    "model_id": "eleven_turbo_v2_5",
                    "voice_settings": {
                        "stability": 0.3,
                        "similarity_boost": 0.75,
                        "style": 0.5,
                        "use_speaker_boost": True,
                    },
                },
            )
            resp.raise_for_status()
            return resp.content
        except Exception:
            logger.exception("ElevenLabs API error")
            return None
