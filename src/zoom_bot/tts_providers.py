"""TTS provider abstraction and implementations.

Provides a pluggable interface for text-to-speech synthesis.
"""

from __future__ import annotations

import base64
import logging
import wave
from abc import ABC, abstractmethod
from io import BytesIO

import httpx

logger = logging.getLogger(__name__)


class TTSProvider(ABC):
    """Abstract base class for text-to-speech providers."""

    @abstractmethod
    async def synthesize(self, text: str) -> tuple[bytes, int] | None:
        """Synthesize speech from text.

        Returns:
            A tuple of (raw_pcm_s16le_mono, sample_rate) or None on failure.
        """


class ElevenLabsTTS(TTSProvider):
    """ElevenLabs cloud TTS API."""

    API_URL = "https://api.elevenlabs.io/v1/text-to-speech"
    DEFAULT_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"  # Sarah

    def __init__(self, api_key: str, voice_id: str = ""):
        self._api_key = api_key
        self._voice_id = voice_id or self.DEFAULT_VOICE_ID
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def synthesize(self, text: str) -> tuple[bytes, int] | None:
        client = await self._ensure_client()
        try:
            # output_format MUST be a query parameter (not in JSON body),
            # otherwise ElevenLabs ignores it and returns MP3 by default.
            resp = await client.post(
                f"{self.API_URL}/{self._voice_id}?output_format=pcm_22050",
                headers={
                    "xi-api-key": self._api_key,
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
            return (resp.content, 22050)
        except Exception:
            logger.exception("ElevenLabs API error")
            return None


class QwenTTS(TTSProvider):
    """Qwen3-TTS via the tts-service API (toddllm:8790).

    POST /v1/synthesize returns {audio_base64: "<base64 WAV>", sample_rate: 24000}.
    """

    def __init__(self, url: str = "http://toddllm:8790", voice: str = "Vivian", language: str = "English"):
        self._url = url.rstrip("/")
        self._voice = voice
        self._language = language
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=90)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def synthesize(self, text: str) -> tuple[bytes, int] | None:
        client = await self._ensure_client()
        try:
            resp = await client.post(
                f"{self._url}/v1/synthesize",
                json={
                    "text": text,
                    "voice": self._voice,
                    "language": self._language,
                    "save_audio": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()

            audio_b64 = data.get("audio_base64", "")
            sample_rate = data.get("sample_rate", 24000)

            if not audio_b64:
                logger.error("Qwen TTS returned empty audio_base64")
                return None

            # Decode base64 WAV → extract raw PCM
            wav_bytes = base64.b64decode(audio_b64)
            pcm_data, wav_rate = self._extract_pcm_from_wav(wav_bytes)
            if pcm_data is None:
                return None

            return (pcm_data, wav_rate or sample_rate)

        except Exception:
            logger.exception("Qwen TTS API error")
            return None

    @staticmethod
    def _extract_pcm_from_wav(wav_bytes: bytes) -> tuple[bytes | None, int | None]:
        """Extract raw s16le PCM data and sample rate from a WAV file."""
        try:
            buf = BytesIO(wav_bytes)
            with wave.open(buf, "rb") as wf:
                sample_rate = wf.getframerate()
                n_frames = wf.getnframes()
                pcm_data = wf.readframes(n_frames)
                return (pcm_data, sample_rate)
        except Exception:
            logger.exception("Failed to parse WAV from Qwen TTS")
            return (None, None)
