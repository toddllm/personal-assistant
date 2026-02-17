"""Discord AI Voice Bot — FastAPI control plane + Discord bot.

Provides REST endpoints to control the bot alongside Discord slash commands.

Run with: uvicorn discord_bot.main:app --host 0.0.0.0 --port 8796
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from discord_bot.bot import DiscordVoiceBot
from discord_bot.commands import register_commands
from discord_bot.config import Settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider factory (reuses zoom_bot's provider implementations)
# ---------------------------------------------------------------------------

def _create_providers(settings: Settings):
    """Create STT/LLM/TTS providers from Discord bot settings.

    Reuses the same provider classes as the Zoom bot.
    """
    from zoom_bot.llm_providers import OpenAICompatibleLLM
    from zoom_bot.stt_providers import DeepgramSTT, FasterWhisperBatchSTT, WhisperStreamingSTT
    from zoom_bot.tts_providers import ElevenLabsTTS, QwenTTS

    # STT
    stt_provider = settings.stt_provider.lower()
    if stt_provider == "deepgram":
        stt = DeepgramSTT(api_key=settings.deepgram_api_key)
    elif stt_provider in ("whisper_streaming", "whisper"):
        stt = WhisperStreamingSTT(url=settings.stt_whisper_url, model=settings.stt_whisper_model)
    elif stt_provider in ("faster_whisper", "whisper_local", "local"):
        stt = FasterWhisperBatchSTT(model=settings.stt_whisper_model, silence_ms=int(settings.debounce_seconds * 1000))
    else:
        raise ValueError(f"Unknown STT provider: {stt_provider}")

    # LLM
    llm_provider = settings.llm_provider.lower()
    if llm_provider == "groq":
        llm = OpenAICompatibleLLM(
            base_url="https://api.groq.com/openai",
            model="llama-3.3-70b-versatile",
            api_key=settings.groq_api_key,
            timeout=settings.llm_timeout,
        )
    elif llm_provider in ("ollama", "openai_compatible"):
        llm = OpenAICompatibleLLM(
            base_url=settings.llm_url,
            model=settings.llm_model,
            api_key="",
            timeout=settings.llm_timeout,
        )
    else:
        raise ValueError(f"Unknown LLM provider: {llm_provider}")

    # TTS
    tts_provider = settings.tts_provider.lower()
    if tts_provider == "elevenlabs":
        tts = ElevenLabsTTS(api_key=settings.elevenlabs_api_key, voice_id=settings.elevenlabs_voice_id)
    elif tts_provider == "qwen":
        tts = QwenTTS(
            url=settings.tts_qwen_url,
            voice=settings.tts_qwen_voice,
            language=settings.tts_qwen_language,
        )
    else:
        raise ValueError(f"Unknown TTS provider: {tts_provider}")

    return stt, llm, tts


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class JoinRequest(BaseModel):
    guild_id: int
    channel_id: int


class StatusResponse(BaseModel):
    connected: bool = False
    guild_id: int | None = None
    channel_id: int | None = None
    uptime_seconds: float | None = None
    stt_provider: str = ""
    llm_provider: str = ""
    tts_provider: str = ""
    active_speakers: int | None = None


class VoiceSettingResponse(BaseModel):
    voice: str = ""
    available_voices: list[str] = Field(default_factory=list)


class VoiceSettingRequest(BaseModel):
    voice: str


class HealthResponse(BaseModel):
    status: str = "ok"
    connected: bool = False


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

settings = Settings()
_voice_bot: DiscordVoiceBot | None = None
_bot_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _voice_bot, _bot_task

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("Discord bot service starting on port %d", settings.port)

    # Create providers and bot
    stt, llm, tts = _create_providers(settings)
    _voice_bot = DiscordVoiceBot(settings, stt, llm, tts)
    register_commands(_voice_bot)

    # Start Discord bot in background task
    if settings.bot_token:
        _bot_task = asyncio.create_task(_voice_bot.start())
        logger.info("Discord bot starting...")
    else:
        logger.warning("No DISCORD_BOT_BOT_TOKEN set — bot will not connect to Discord")

    yield

    # Cleanup
    if _voice_bot:
        await _voice_bot.close()
    if _bot_task:
        _bot_task.cancel()
    logger.info("Discord bot service stopped")


app = FastAPI(
    title="Discord AI Voice Bot",
    description="Discord voice bot with pluggable STT/LLM/TTS providers",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse)
async def health():
    connected = _voice_bot.is_connected if _voice_bot else False
    return HealthResponse(connected=connected)


@app.get("/status", response_model=StatusResponse)
async def status():
    if not _voice_bot:
        return StatusResponse()
    info = _voice_bot.status_info
    return StatusResponse(**info)


@app.post("/join", response_model=StatusResponse)
async def join(req: JoinRequest):
    if not _voice_bot:
        raise HTTPException(500, "Bot not initialized")
    if _voice_bot.is_connected:
        raise HTTPException(400, "Already in a voice channel. Leave first.")

    try:
        await _voice_bot.join_channel(req.guild_id, req.channel_id)
    except Exception as exc:
        logger.exception("Failed to join voice channel")
        raise HTTPException(500, f"Failed to join: {exc}")

    info = _voice_bot.status_info
    return StatusResponse(**info)


@app.post("/leave", response_model=StatusResponse)
async def leave():
    if not _voice_bot:
        raise HTTPException(500, "Bot not initialized")
    if not _voice_bot.is_connected:
        raise HTTPException(400, "Not in a voice channel.")

    await _voice_bot.leave_channel()
    return StatusResponse()


@app.get("/voice", response_model=VoiceSettingResponse)
async def get_voice():
    """Get the current TTS voice and list of available voices."""
    current = settings.tts_qwen_voice
    # Try to fetch available voices from TTS service
    available: list[str] = []
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{settings.tts_qwen_url}/v1/profiles")
            if r.status_code == 200:
                data = r.json()
                available = [p["name"] for p in data.get("profiles", [])]
    except Exception:
        pass
    # Also report the runtime voice if TTS provider is live
    if _voice_bot and hasattr(_voice_bot, "_tts") and hasattr(_voice_bot._tts, "_voice"):
        current = _voice_bot._tts._voice
    return VoiceSettingResponse(voice=current, available_voices=available)


@app.put("/voice", response_model=VoiceSettingResponse)
async def set_voice(req: VoiceSettingRequest):
    """Change the TTS voice at runtime (no restart needed)."""
    new_voice = req.voice.strip()
    if not new_voice:
        raise HTTPException(400, "Voice name cannot be empty.")
    # Update the live TTS provider
    if _voice_bot and hasattr(_voice_bot, "_tts") and hasattr(_voice_bot._tts, "_voice"):
        _voice_bot._tts._voice = new_voice
        logger.info("Discord bot TTS voice changed to: %s", new_voice)
    else:
        raise HTTPException(503, "TTS provider not available.")
    # Also update settings so status reflects it
    settings.tts_qwen_voice = new_voice
    return VoiceSettingResponse(voice=new_voice)


def main():
    """Entry point for the discord-bot command."""
    import uvicorn
    uvicorn.run(
        "discord_bot.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
