"""STT provider abstraction and implementations.

Provides a pluggable interface for speech-to-text services.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from typing import Callable

logger = logging.getLogger(__name__)


class STTProvider(ABC):
    """Abstract base class for speech-to-text providers."""

    @abstractmethod
    async def start(self) -> None:
        """Start the STT provider (connect, initialize)."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the STT provider and release resources."""

    @abstractmethod
    async def send_audio(self, pcm_16k_s16le: bytes) -> None:
        """Send a chunk of 16kHz s16le mono PCM audio for transcription."""

    def set_transcript_callback(self, cb: Callable[[str], None]) -> None:
        """Set the callback invoked when a final transcript is ready."""
        self._transcript_callback = cb

    def _emit_transcript(self, text: str) -> None:
        cb = getattr(self, "_transcript_callback", None)
        if cb:
            cb(text)


class DeepgramSTT(STTProvider):
    """Deepgram real-time STT via WebSocket (nova-2)."""

    WS_URL = "wss://api.deepgram.com/v1/listen"
    SAMPLE_RATE = 16000

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._ws = None
        self._running = False
        self._recv_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._recv_task = asyncio.create_task(self._connection_loop())

    async def stop(self) -> None:
        self._running = False
        if self._recv_task:
            self._recv_task.cancel()
            self._recv_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def send_audio(self, pcm_16k_s16le: bytes) -> None:
        if self._ws:
            try:
                await self._ws.send(pcm_16k_s16le)
            except Exception:
                logger.debug("Failed to send audio to Deepgram")

    async def _connection_loop(self) -> None:
        import websockets

        params = (
            f"?encoding=linear16&sample_rate={self.SAMPLE_RATE}"
            f"&channels=1&model=nova-2&punctuate=true"
            f"&interim_results=false&endpointing=500"
            f"&vad_events=true&smart_format=true"
        )

        while self._running:
            try:
                headers = {"Authorization": f"Token {self._api_key}"}
                async with websockets.connect(
                    self.WS_URL + params,
                    additional_headers=headers,
                    ping_interval=20,
                ) as ws:
                    self._ws = ws
                    logger.info("Deepgram WebSocket connected")
                    async for msg in ws:
                        if not self._running:
                            break
                        self._handle_message(msg)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Deepgram connection error, reconnecting...")
                self._ws = None
                await asyncio.sleep(2)

    def _handle_message(self, msg: str | bytes) -> None:
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
        self._emit_transcript(transcript)


class FasterWhisperBatchSTT(STTProvider):
    """In-process batch STT using faster-whisper (CTranslate2).

    Uses energy-based voice activity detection: accumulates audio only
    while speech energy is above threshold, then transcribes after
    `silence_ms` of quiet. Handles continuous mic streams correctly.
    """

    # Energy thresholds (RMS of float32 samples, range 0.0–1.0)
    # Default tuned for headphone use (no echo risk, lower threshold OK).
    # For speaker playback, raise to 0.03 to avoid picking up TV/room noise.
    SPEECH_THRESHOLD = 0.008   # above = speech detected
    SILENCE_THRESHOLD = 0.004  # below = silence

    def __init__(self, model: str = "base.en", silence_ms: int = 800,
                 speech_threshold: float | None = None, silence_threshold: float | None = None):
        self._model_name = model
        self._silence_ms = silence_ms
        if speech_threshold is not None:
            self.SPEECH_THRESHOLD = speech_threshold
        if silence_threshold is not None:
            self.SILENCE_THRESHOLD = silence_threshold
        self._model = None
        self._speech_buffer = bytearray()  # accumulated speech audio
        self._running = False
        self._in_speech = False
        self._silence_start: float = 0.0  # when silence began after speech
        self._check_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._running = True
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._load_model)
        self._check_task = asyncio.create_task(self._process_loop())

    def _load_model(self) -> None:
        from faster_whisper import WhisperModel
        logger.info("Loading faster-whisper model '%s'...", self._model_name)
        self._model = WhisperModel(self._model_name, device="cpu", compute_type="int8")
        logger.info("faster-whisper model loaded")

    async def stop(self) -> None:
        self._running = False
        if self._check_task:
            self._check_task.cancel()
            self._check_task = None
        self._model = None

    async def send_audio(self, pcm_16k_s16le: bytes) -> None:
        """Receive audio chunk, detect speech/silence transitions."""
        import numpy as np

        # Compute RMS energy of this chunk
        samples = np.frombuffer(pcm_16k_s16le, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples ** 2)))
        now = asyncio.get_event_loop().time()

        async with self._lock:
            if rms >= self.SPEECH_THRESHOLD:
                # Speech detected
                if not self._in_speech:
                    self._in_speech = True
                    logger.debug("Speech started (RMS=%.4f)", rms)
                self._speech_buffer.extend(pcm_16k_s16le)
                self._silence_start = 0.0
            elif self._in_speech:
                # Was speaking, now quiet — keep buffering for a bit
                # (captures trailing audio)
                self._speech_buffer.extend(pcm_16k_s16le)
                if self._silence_start == 0.0:
                    self._silence_start = now
            # else: not in speech and quiet → ignore

    async def _process_loop(self) -> None:
        """Check for end-of-utterance and transcribe."""
        import numpy as np

        while self._running:
            await asyncio.sleep(0.05)
            now = asyncio.get_event_loop().time()

            async with self._lock:
                if not self._in_speech:
                    continue
                if self._silence_start == 0.0:
                    continue  # still speaking

                silence_ms = (now - self._silence_start) * 1000
                if silence_ms < self._silence_ms:
                    continue  # not enough silence yet

                # End of utterance — grab speech buffer
                if len(self._speech_buffer) < 3200:  # < 100ms, skip
                    self._speech_buffer.clear()
                    self._in_speech = False
                    self._silence_start = 0.0
                    continue

                pcm_bytes = bytes(self._speech_buffer)
                self._speech_buffer.clear()
                self._in_speech = False
                self._silence_start = 0.0

            if not self._model:
                continue

            audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            duration = len(audio) / 16000
            logger.info("Transcribing %.1fs of speech...", duration)

            loop = asyncio.get_event_loop()
            text = await loop.run_in_executor(None, self._transcribe, audio)
            if text:
                logger.info("Transcript: %s", text)
                self._emit_transcript(text)

    def _transcribe(self, audio_float32) -> str:
        """Run faster-whisper inference on audio samples."""
        try:
            segments, info = self._model.transcribe(
                audio_float32,
                beam_size=1,
                language="en",
                vad_filter=True,
                no_speech_threshold=0.6,
            )
            parts = []
            for seg in segments:
                text = seg.text.strip()
                if text:
                    parts.append(text)
            return " ".join(parts)
        except Exception:
            logger.exception("Whisper transcription error")
            return ""


class RemoteWhisperSTT(STTProvider):
    """Remote batch STT via HTTP — sends PCM to a faster-whisper GPU server.

    VAD runs locally (same energy-based detection as FasterWhisperBatchSTT),
    but transcription is done on a remote GPU via POST /v1/transcribe.
    """

    SPEECH_THRESHOLD = 0.008
    SILENCE_THRESHOLD = 0.004

    def __init__(self, url: str = "http://toddllm:8791", silence_ms: int = 800,
                 speech_threshold: float | None = None, silence_threshold: float | None = None):
        self._url = url.rstrip("/")
        self._silence_ms = silence_ms
        if speech_threshold is not None:
            self.SPEECH_THRESHOLD = speech_threshold
        if silence_threshold is not None:
            self.SILENCE_THRESHOLD = silence_threshold
        self._speech_buffer = bytearray()
        self._running = False
        self._in_speech = False
        self._silence_start: float = 0.0
        self._check_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._client = None

    async def start(self) -> None:
        import httpx
        self._running = True
        self._client = httpx.AsyncClient(timeout=30.0)
        self._check_task = asyncio.create_task(self._process_loop())
        logger.info("RemoteWhisperSTT started (endpoint: %s)", self._url)

    async def stop(self) -> None:
        self._running = False
        if self._check_task:
            self._check_task.cancel()
            self._check_task = None
        if self._client:
            await self._client.aclose()
            self._client = None

    async def send_audio(self, pcm_16k_s16le: bytes) -> None:
        """Receive audio chunk, detect speech/silence transitions (local VAD)."""
        import numpy as np

        samples = np.frombuffer(pcm_16k_s16le, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples ** 2)))
        now = asyncio.get_event_loop().time()

        async with self._lock:
            if rms >= self.SPEECH_THRESHOLD:
                if not self._in_speech:
                    self._in_speech = True
                    logger.debug("Speech started (RMS=%.4f)", rms)
                self._speech_buffer.extend(pcm_16k_s16le)
                self._silence_start = 0.0
            elif self._in_speech:
                self._speech_buffer.extend(pcm_16k_s16le)
                if self._silence_start == 0.0:
                    self._silence_start = now

    async def _process_loop(self) -> None:
        """Check for end-of-utterance and transcribe via remote server."""
        while self._running:
            await asyncio.sleep(0.05)
            now = asyncio.get_event_loop().time()

            async with self._lock:
                if not self._in_speech:
                    continue
                if self._silence_start == 0.0:
                    continue
                silence_ms = (now - self._silence_start) * 1000
                if silence_ms < self._silence_ms:
                    continue
                if len(self._speech_buffer) < 3200:
                    self._speech_buffer.clear()
                    self._in_speech = False
                    self._silence_start = 0.0
                    continue

                pcm_bytes = bytes(self._speech_buffer)
                self._speech_buffer.clear()
                self._in_speech = False
                self._silence_start = 0.0

            duration = len(pcm_bytes) / 2 / 16000
            logger.info("Sending %.1fs of speech to remote STT...", duration)

            try:
                resp = await self._client.post(
                    f"{self._url}/v1/transcribe",
                    content=pcm_bytes,
                    headers={"Content-Type": "application/octet-stream"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    text = data.get("text", "").strip()
                    inference_s = data.get("inference_s", 0)
                    if text:
                        logger.info("Remote transcript (%.2fs inference): %s", inference_s, text)
                        self._emit_transcript(text)
                else:
                    logger.error("Remote STT error %d: %s", resp.status_code, resp.text[:200])
            except Exception:
                logger.exception("Remote STT request failed")


class WhisperStreamingSTT(STTProvider):
    """Streaming Whisper STT via speaches WebSocket.

    Connects to a speaches-ai/speaches server that provides
    WS /v1/audio/transcriptions accepting raw 16kHz s16le PCM.
    """

    def __init__(self, url: str = "ws://127.0.0.1:8000/v1/audio/transcriptions", model: str = "small.en"):
        self._url = url
        self._model = model
        self._ws = None
        self._running = False
        self._recv_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._recv_task = asyncio.create_task(self._connection_loop())

    async def stop(self) -> None:
        self._running = False
        if self._recv_task:
            self._recv_task.cancel()
            self._recv_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def send_audio(self, pcm_16k_s16le: bytes) -> None:
        if self._ws:
            try:
                await self._ws.send(pcm_16k_s16le)
            except Exception:
                logger.debug("Failed to send audio to Whisper STT")

    async def _connection_loop(self) -> None:
        import websockets

        # Speaches expects query params for audio format
        url = (
            f"{self._url}"
            f"?model={self._model}"
            f"&language=en"
            f"&response_format=json"
            f"&stream=true"
        )

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    self._ws = ws
                    logger.info("Whisper STT WebSocket connected to %s", self._url)
                    async for msg in ws:
                        if not self._running:
                            break
                        self._handle_message(msg)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Whisper STT connection error, reconnecting...")
                self._ws = None
                await asyncio.sleep(2)

    def _handle_message(self, msg: str | bytes) -> None:
        if isinstance(msg, bytes):
            return
        try:
            data = json.loads(msg)
        except (json.JSONDecodeError, TypeError):
            return

        # Speaches returns {"text": "...", "is_final": true/false}
        # or {"segments": [{"text": "..."}], ...}
        text = ""
        if "text" in data:
            text = data["text"].strip()
        elif "segments" in data:
            segments = data.get("segments", [])
            text = " ".join(s.get("text", "").strip() for s in segments).strip()

        if not text:
            return

        # Only emit final transcripts
        is_final = data.get("is_final", True)
        if not is_final:
            return

        logger.info("Transcript: %s", text)
        self._emit_transcript(text)
