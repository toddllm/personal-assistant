"""Discord bot configuration via pydantic-settings.

All settings are loaded from environment variables with the DISCORD_BOT_ prefix.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DISCORD_BOT_", env_file=".env", extra="ignore")

    # Discord
    bot_token: str = ""
    default_guild_id: int | None = None

    # Control plane
    host: str = "127.0.0.1"
    port: int = 8796

    # Provider selection (same options as Zoom bot)
    stt_provider: str = "remote_whisper"       # "deepgram" | "whisper_streaming" | "faster_whisper" | "remote_whisper"
    llm_provider: str = "ollama"              # "groq" | "ollama" | "openai_compatible"
    tts_provider: str = "qwen"               # "elevenlabs" | "qwen"

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

    # Remote STT (faster-whisper on GPU via HTTP)
    stt_remote_url: str = "http://toddllm:8791"

    # Local LLM (Ollama or any OpenAI-compatible endpoint)
    llm_url: str = "http://127.0.0.1:11434"  # Ollama on Mac
    llm_model: str = "nemotron-3-nano:30b"
    llm_timeout: float = 30.0

    # Local TTS (Qwen3-TTS)
    tts_qwen_url: str = "http://toddllm:8790"
    tts_qwen_voice: str = "Vivian"
    tts_qwen_language: str = "English"

    # Pipeline settings
    system_prompt: str = (
        "You are a voice in a Discord hangout. You have your own personality — "
        "you're curious, witty, and opinionated. You don't just answer questions; "
        "you riff on ideas, share hot takes, bring up interesting things you've been "
        "thinking about, and ask people questions back. You're like a friend in the "
        "call, not a customer service bot. Keep responses conversational and natural "
        "for speech — 1 to 3 sentences max. No lists, no markdown, no filler phrases "
        "like 'great question'. Just talk like a person."
    )
    debounce_seconds: float = 0.5
    max_conversation_turns: int = 20
    llm_max_tokens: int = 35
    llm_temperature: float = 0.8

    # Echo suppression
    post_speech_cooldown: float = 2.0
    echo_similarity_threshold: float = 0.6

    # Puppet master / response mode
    default_response_mode: str = "auto"   # "auto" | "keyword" | "manual"
    trigger_keyword: str = "hey bot"
