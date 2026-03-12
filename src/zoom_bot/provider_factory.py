"""Factory for creating provider instances from configuration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zoom_bot.bot_config import ZoomBotSettings
    from zoom_bot.llm_providers import LLMProvider
    from zoom_bot.stt_providers import STTProvider
    from zoom_bot.tts_providers import TTSProvider

logger = logging.getLogger(__name__)


def create_stt(settings: ZoomBotSettings) -> STTProvider:
    """Create an STT provider based on settings."""
    from zoom_bot.stt_providers import DeepgramSTT, WhisperStreamingSTT

    provider = settings.stt_provider.lower()
    if provider == "deepgram":
        if not settings.deepgram_api_key:
            raise ValueError("ZOOM_BOT_DEEPGRAM_API_KEY required for Deepgram STT")
        logger.info("STT provider: Deepgram (nova-2)")
        return DeepgramSTT(api_key=settings.deepgram_api_key)
    elif provider in ("whisper_streaming", "whisper"):
        logger.info("STT provider: Whisper Streaming (%s at %s)", settings.stt_whisper_model, settings.stt_whisper_url)
        return WhisperStreamingSTT(url=settings.stt_whisper_url, model=settings.stt_whisper_model)
    else:
        raise ValueError(f"Unknown STT provider: {provider}")


def create_llm(settings: ZoomBotSettings) -> LLMProvider:
    """Create an LLM provider based on settings."""
    from zoom_bot.llm_providers import OpenAICompatibleLLM

    provider = settings.llm_provider.lower()
    if provider == "groq":
        if not settings.groq_api_key:
            raise ValueError("ZOOM_BOT_GROQ_API_KEY required for Groq LLM")
        logger.info("LLM provider: Groq (llama-3.3-70b-versatile)")
        return OpenAICompatibleLLM(
            base_url="https://api.groq.com/openai",
            model="llama-3.3-70b-versatile",
            api_key=settings.groq_api_key,
            timeout=settings.llm_timeout,
        )
    elif provider in ("ollama", "openai_compatible"):
        logger.info("LLM provider: %s (%s at %s)", provider, settings.llm_model, settings.llm_url)
        return OpenAICompatibleLLM(
            base_url=settings.llm_url,
            model=settings.llm_model,
            api_key="",
            timeout=settings.llm_timeout,
        )
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")


def create_tts(settings: ZoomBotSettings) -> TTSProvider:
    """Create a TTS provider based on settings."""
    from zoom_bot.tts_providers import ElevenLabsTTS, QwenTTS

    provider = settings.tts_provider.lower()
    if provider == "elevenlabs":
        if not settings.elevenlabs_api_key:
            raise ValueError("ZOOM_BOT_ELEVENLABS_API_KEY required for ElevenLabs TTS")
        logger.info("TTS provider: ElevenLabs (voice=%s)", settings.elevenlabs_voice_id)
        return ElevenLabsTTS(api_key=settings.elevenlabs_api_key, voice_id=settings.elevenlabs_voice_id)
    elif provider == "qwen":
        logger.info("TTS provider: Qwen3 (%s at %s)", settings.tts_qwen_voice, settings.tts_qwen_url)
        return QwenTTS(
            url=settings.tts_qwen_url,
            voice=settings.tts_qwen_voice,
            language=settings.tts_qwen_language,
        )
    else:
        raise ValueError(f"Unknown TTS provider: {provider}")


def create_providers(settings: ZoomBotSettings) -> tuple[STTProvider, LLMProvider, TTSProvider]:
    """Create all three providers from settings.

    Returns:
        (stt, llm, tts) tuple of provider instances.
    """
    stt = create_stt(settings)
    llm = create_llm(settings)
    tts = create_tts(settings)
    return stt, llm, tts
