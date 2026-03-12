"""Zoom bot configuration via pydantic-settings.

All settings are loaded from environment variables with the ZOOM_BOT_ prefix.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class ZoomBotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ZOOM_BOT_", extra="ignore")

    # Provider selection
    stt_provider: str = "deepgram"           # "deepgram" | "whisper_streaming"
    llm_provider: str = "groq"               # "groq" | "ollama" | "openai_compatible"
    tts_provider: str = "elevenlabs"         # "elevenlabs" | "qwen"

    # Cloud API keys (only needed for respective cloud providers)
    deepgram_api_key: str = ""
    groq_api_key: str = ""
    elevenlabs_api_key: str = ""

    # Deepgram settings
    deepgram_model: str = "nova-2"

    # ElevenLabs settings
    elevenlabs_voice_id: str = "EXAVITQu4vr4xnSDxMaL"  # Sarah

    # Local STT (speaches / faster-whisper-server)
    stt_whisper_url: str = "ws://127.0.0.1:8000/v1/audio/transcriptions"
    stt_whisper_model: str = "small.en"

    # Local LLM (Ollama or any OpenAI-compatible endpoint)
    llm_url: str = "http://host.orb.internal:11434"  # Ollama on Mac
    llm_model: str = "llama3.1:8b"
    llm_timeout: float = 30.0

    # Local TTS (Qwen3-TTS on toddllm)
    tts_qwen_url: str = "http://toddllm:8790"
    tts_qwen_voice: str = "Vivian"
    tts_qwen_language: str = "English"

    # System prompt
    system_prompt: str = (
        "You are a friendly AI assistant in a Zoom meeting. "
        "IMPORTANT: Keep every response to ONE short sentence (under 15 words). "
        "Be conversational and natural. Never give long explanations. "
        "If asked who you are, say you're an AI assistant here to help."
    )

    # Pipeline settings
    debounce_seconds: float = 0.8
    max_conversation_turns: int = 20
    llm_max_tokens: int = 80
    llm_temperature: float = 0.5
