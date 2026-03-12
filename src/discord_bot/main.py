"""Discord AI Voice Bot — FastAPI control plane + Discord bot.

Provides REST endpoints to control the bot alongside Discord slash commands.

Run with: uvicorn discord_bot.main:app --host 0.0.0.0 --port 8796
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import discord

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
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
    from zoom_bot.stt_providers import DeepgramSTT, FasterWhisperBatchSTT, RemoteWhisperSTT, WhisperStreamingSTT
    from zoom_bot.tts_providers import ElevenLabsTTS, QwenTTS

    # STT
    stt_provider = settings.stt_provider.lower()
    if stt_provider == "deepgram":
        stt = DeepgramSTT(api_key=settings.deepgram_api_key)
    elif stt_provider in ("whisper_streaming", "whisper"):
        stt = WhisperStreamingSTT(url=settings.stt_whisper_url, model=settings.stt_whisper_model)
    elif stt_provider in ("faster_whisper", "whisper_local", "local"):
        stt = FasterWhisperBatchSTT(model=settings.stt_whisper_model, silence_ms=int(settings.debounce_seconds * 1000))
    elif stt_provider in ("remote_whisper", "remote"):
        stt = RemoteWhisperSTT(url=settings.stt_remote_url, silence_ms=int(settings.debounce_seconds * 1000))
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
    guild_id: str  # String to preserve JS precision for snowflake IDs
    channel_id: str


class StatusResponse(BaseModel):
    connected: bool = False
    guild_id: int | None = None
    channel_id: int | None = None
    uptime_seconds: float | None = None
    stt_provider: str = ""
    llm_provider: str = ""
    tts_provider: str = ""
    active_speakers: int | None = None


class VoiceOption(BaseModel):
    name: str
    display_name: str
    voice_type: str = "builtin"
    is_owner: bool = False


class VoiceSettingResponse(BaseModel):
    voice: str = ""
    available_voices: list[VoiceOption] = Field(default_factory=list)


class VoiceSettingRequest(BaseModel):
    voice: str


class BotSettingsResponse(BaseModel):
    # TTS
    tts_voice: str = ""
    tts_url: str = ""
    tts_language: str = "English"
    # LLM
    llm_model: str = ""
    llm_url: str = ""
    llm_temperature: float = 0.8
    llm_max_tokens: int = 120
    # Pipeline
    system_prompt: str = ""
    debounce_seconds: float = 0.5


class BotSettingsRequest(BaseModel):
    tts_voice: str | None = None
    llm_model: str | None = None
    llm_temperature: float | None = None
    llm_max_tokens: int | None = None
    system_prompt: str | None = None
    debounce_seconds: float | None = None


class SpeakRequest(BaseModel):
    text: str


class InjectRequest(BaseModel):
    text: str
    role: str = "system"


class NudgeRequest(BaseModel):
    hint: str


class ResponseModeRequest(BaseModel):
    mode: str  # "auto" | "keyword" | "manual"
    keyword: str = ""


class ReloadSTTRequest(BaseModel):
    model: str = ""  # e.g. "medium.en", "large-v3"; empty = use current setting


class RetranscribeRequest(BaseModel):
    model: str = "medium.en"


class HealthResponse(BaseModel):
    status: str = "ok"
    connected: bool = False


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

settings = Settings()
_voice_bot: DiscordVoiceBot | None = None
_bot_task: asyncio.Task | None = None
_events: deque[dict] = deque(maxlen=200)


def _on_pipeline_event(event_type: str, data: dict) -> None:
    """Callback fired by AudioPipeline for transcript/llm/tts events."""
    _events.append(data)

_RUNTIME_SETTINGS_PATH = Path("data/discord-bot-settings.json")


def _load_runtime_overrides() -> None:
    """Apply saved runtime overrides (voice, model, etc.) to settings."""
    if not _RUNTIME_SETTINGS_PATH.exists():
        return
    try:
        overrides = json.loads(_RUNTIME_SETTINGS_PATH.read_text())
        for key in (
            "tts_qwen_voice", "llm_model", "llm_temperature", "llm_max_tokens",
            "system_prompt", "stt_whisper_model",
            "post_speech_cooldown", "echo_similarity_threshold",
            "default_response_mode", "trigger_keyword",
        ):
            if key in overrides:
                setattr(settings, key, overrides[key])
        logger.info("Loaded runtime overrides: %s", list(overrides.keys()))
    except Exception:
        logger.exception("Failed to load runtime overrides")


def _save_runtime_overrides(**kwargs: object) -> None:
    """Persist runtime settings so they survive restarts."""
    existing: dict = {}
    if _RUNTIME_SETTINGS_PATH.exists():
        try:
            existing = json.loads(_RUNTIME_SETTINGS_PATH.read_text())
        except Exception:
            pass
    existing.update(kwargs)
    _RUNTIME_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _RUNTIME_SETTINGS_PATH.write_text(json.dumps(existing, indent=2))


_load_runtime_overrides()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _voice_bot, _bot_task

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Load Opus codec for voice decoding (py-cord needs this for receiving audio)
    if not discord.opus.is_loaded():
        _opus_paths = [
            "/opt/homebrew/lib/libopus.dylib",          # macOS Homebrew ARM
            "/usr/local/lib/libopus.dylib",              # macOS Homebrew Intel
            "/usr/lib/x86_64-linux-gnu/libopus.so.0",   # Debian/Ubuntu
            "libopus",                                    # system default
        ]
        for path in _opus_paths:
            try:
                discord.opus.load_opus(path)
                logger.info("Loaded Opus codec from %s", path)
                break
            except OSError:
                continue
        if not discord.opus.is_loaded():
            logger.warning("Could not load Opus codec — incoming voice audio will not work")

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

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(CONTROL_PANEL_UI)


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
        guild_id = int(req.guild_id)
        channel_id = int(req.channel_id)
        await _voice_bot.join_channel(guild_id, channel_id)
        # Wire pipeline event callback
        if _voice_bot.pipeline:
            _voice_bot.pipeline.set_event_callback(_on_pipeline_event)
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


@app.get("/guilds")
async def list_guilds():
    """List guilds and their voice channels the bot can see."""
    if not _voice_bot:
        return {"guilds": []}
    result = []
    for guild in _voice_bot.bot.guilds:
        channels = []
        for ch in guild.channels:
            if isinstance(ch, discord.VoiceChannel):
                members = [m.display_name for m in ch.members if not m.bot]
                channels.append({
                    "id": str(ch.id), "name": ch.name,
                    "members": members, "member_count": len(members),
                })
        result.append({
            "id": str(guild.id), "name": guild.name,
            "channels": channels,
        })
    return {"guilds": result}


@app.get("/voice", response_model=VoiceSettingResponse)
async def get_voice():
    """Get the current TTS voice and list of available voices."""
    current = settings.tts_qwen_voice
    # Try to fetch available voices from TTS service
    available: list[VoiceOption] = []
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{settings.tts_qwen_url}/v1/profiles")
            if r.status_code == 200:
                data = r.json()
                for p in data.get("profiles", []):
                    available.append(VoiceOption(
                        name=p["name"],
                        display_name=p.get("display_name", p["name"]),
                        voice_type=p.get("voice_type", "builtin"),
                        is_owner=p.get("is_owner", False),
                    ))
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
    _save_runtime_overrides(tts_qwen_voice=new_voice)
    return VoiceSettingResponse(voice=new_voice)


@app.get("/settings", response_model=BotSettingsResponse)
async def get_settings():
    """Get current bot runtime settings."""
    resp = BotSettingsResponse(
        tts_url=settings.tts_qwen_url,
        tts_language=settings.tts_qwen_language,
        tts_voice=settings.tts_qwen_voice,
        llm_url=settings.llm_url,
        llm_model=settings.llm_model,
        llm_temperature=settings.llm_temperature,
        llm_max_tokens=settings.llm_max_tokens,
        system_prompt=settings.system_prompt,
        debounce_seconds=settings.debounce_seconds,
    )
    # Override with live runtime values from providers when available
    if _voice_bot:
        if hasattr(_voice_bot._tts, "_voice"):
            resp.tts_voice = _voice_bot._tts._voice
        if hasattr(_voice_bot._llm, "_model"):
            resp.llm_model = _voice_bot._llm._model
        # Also read from live pipeline if running
        if _voice_bot.pipeline:
            resp.llm_temperature = _voice_bot.pipeline._llm_temperature
            resp.llm_max_tokens = _voice_bot.pipeline._llm_max_tokens
            resp.debounce_seconds = _voice_bot.pipeline._debounce_seconds
    return resp


@app.put("/settings", response_model=BotSettingsResponse)
async def update_settings(req: BotSettingsRequest):
    """Update bot settings at runtime (no restart needed)."""
    if not _voice_bot:
        raise HTTPException(503, "Bot not initialized")

    persist: dict = {}

    if req.tts_voice is not None and hasattr(_voice_bot._tts, "_voice"):
        _voice_bot._tts._voice = req.tts_voice
        settings.tts_qwen_voice = req.tts_voice
        persist["tts_qwen_voice"] = req.tts_voice
        logger.info("TTS voice changed to: %s", req.tts_voice)

    if req.llm_model is not None:
        if hasattr(_voice_bot._llm, "_model"):
            old_model = _voice_bot._llm._model
            _voice_bot._llm._model = req.llm_model
            logger.info("LLM model changed: %s -> %s (live provider updated)", old_model, req.llm_model)
        else:
            logger.warning("LLM provider has no _model attribute, cannot update live")
        settings.llm_model = req.llm_model
        persist["llm_model"] = req.llm_model

    if req.llm_temperature is not None:
        settings.llm_temperature = req.llm_temperature
        persist["llm_temperature"] = req.llm_temperature
        # Propagate to live pipeline
        if _voice_bot and _voice_bot.pipeline:
            _voice_bot.pipeline._llm_temperature = req.llm_temperature
        logger.info("LLM temperature changed to: %s", req.llm_temperature)

    if req.llm_max_tokens is not None:
        settings.llm_max_tokens = req.llm_max_tokens
        persist["llm_max_tokens"] = req.llm_max_tokens
        # Propagate to live pipeline
        if _voice_bot and _voice_bot.pipeline:
            _voice_bot.pipeline._llm_max_tokens = req.llm_max_tokens
        logger.info("LLM max_tokens changed to: %s", req.llm_max_tokens)

    if req.system_prompt is not None:
        settings.system_prompt = req.system_prompt
        persist["system_prompt"] = req.system_prompt
        # Propagate to live pipeline — update conversation's system message
        if _voice_bot and _voice_bot.pipeline:
            _voice_bot.pipeline._system_prompt = req.system_prompt
            conv = _voice_bot.pipeline._conversation
            if conv and conv[0].get("role") == "system":
                conv[0]["content"] = req.system_prompt
        logger.info("System prompt updated (%d chars)", len(req.system_prompt))

    if req.debounce_seconds is not None:
        settings.debounce_seconds = req.debounce_seconds
        # Propagate to live pipeline
        if _voice_bot and _voice_bot.pipeline:
            _voice_bot.pipeline._debounce_seconds = req.debounce_seconds

    if persist:
        _save_runtime_overrides(**persist)

    return await get_settings()


@app.get("/events")
async def get_events(since: float = 0):
    """Get pipeline events (transcript, llm_response, tts_audio) since a timestamp."""
    return {"events": [e for e in _events if e.get("ts", 0) > since]}


@app.get("/conversation")
async def get_conversation():
    """Get the current conversation history from the pipeline."""
    if _voice_bot and _voice_bot.pipeline:
        return {"conversation": _voice_bot.pipeline.conversation}
    return {"conversation": []}


@app.get("/ollama/models")
async def list_ollama_models():
    """List available Ollama models."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{settings.llm_url}/api/tags")
            if r.status_code == 200:
                data = r.json()
                models = [m["name"] for m in data.get("models", [])]
                return {"models": sorted(models)}
    except Exception:
        pass
    return {"models": []}


@app.post("/speak")
async def speak(req: SpeakRequest):
    """Operator: make the bot speak text in the voice channel."""
    if not _voice_bot or not _voice_bot.pipeline:
        raise HTTPException(503, "Bot not in a voice channel")
    await _voice_bot.pipeline.speak(req.text)
    return {"ok": True, "text": req.text}


@app.post("/inject")
async def inject(req: InjectRequest):
    """Operator: add context to conversation silently."""
    if not _voice_bot or not _voice_bot.pipeline:
        raise HTTPException(503, "Bot not in a voice channel")
    _voice_bot.pipeline.inject(req.text, req.role)
    return {"ok": True, "role": req.role, "text": req.text}


@app.post("/nudge")
async def nudge(req: NudgeRequest):
    """Operator: queue a one-shot hint for the next response."""
    if not _voice_bot or not _voice_bot.pipeline:
        raise HTTPException(503, "Bot not in a voice channel")
    _voice_bot.pipeline.nudge(req.hint)
    return {"ok": True, "hint": req.hint}


@app.post("/mute")
async def mute():
    """Operator: pause auto-responding (transcripts still recorded)."""
    if not _voice_bot or not _voice_bot.pipeline:
        raise HTTPException(503, "Bot not in a voice channel")
    _voice_bot.pipeline.mute()
    return {"ok": True, "muted": True}


@app.post("/unmute")
async def unmute():
    """Operator: resume auto-responding."""
    if not _voice_bot or not _voice_bot.pipeline:
        raise HTTPException(503, "Bot not in a voice channel")
    _voice_bot.pipeline.unmute()
    return {"ok": True, "muted": False}


@app.post("/cancel")
async def cancel():
    """Operator: abort in-flight response and clear audio buffer."""
    if not _voice_bot or not _voice_bot.pipeline:
        raise HTTPException(503, "Bot not in a voice channel")
    await _voice_bot.pipeline.cancel()
    # Also clear the TTS audio buffer
    if _voice_bot._tts_source:
        _voice_bot._tts_source.clear()
    return {"ok": True}


@app.post("/response-mode")
async def set_response_mode(req: ResponseModeRequest):
    """Operator: set response mode (auto/keyword/manual)."""
    if not _voice_bot or not _voice_bot.pipeline:
        raise HTTPException(503, "Bot not in a voice channel")
    try:
        _voice_bot.pipeline.set_response_mode(req.mode, req.keyword)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _save_runtime_overrides(default_response_mode=req.mode, trigger_keyword=req.keyword or settings.trigger_keyword)
    return {"ok": True, "mode": req.mode, "keyword": _voice_bot.pipeline._trigger_keyword}


@app.get("/pipeline-state")
async def pipeline_state():
    """Get current pipeline state for UI (mute, mode, etc.)."""
    if not _voice_bot or not _voice_bot.pipeline:
        return {
            "active": False, "muted": False, "response_mode": settings.default_response_mode,
            "trigger_keyword": settings.trigger_keyword,
            "post_speech_cooldown": settings.post_speech_cooldown,
            "playback_remaining_ms": 0,
        }
    p = _voice_bot.pipeline
    remaining = 0.0
    if _voice_bot._tts_source:
        remaining = _voice_bot._tts_source.duration_remaining_ms
    return {
        "active": True,
        "muted": p.muted,
        "response_mode": p.response_mode,
        "trigger_keyword": p._trigger_keyword,
        "post_speech_cooldown": p._post_speech_cooldown,
        "playback_remaining_ms": round(remaining, 1),
        "responding": p._responding,
    }


@app.post("/reload-stt")
async def reload_stt(req: ReloadSTTRequest):
    """Hot-swap the STT model without restarting the bot.

    Stops the current STT provider, loads the new model, restarts it,
    and re-wires the transcript callback.
    """
    if not _voice_bot or not _voice_bot.pipeline:
        raise HTTPException(503, "Bot not in a voice channel")

    new_model = req.model.strip() or settings.stt_whisper_model
    old_model = settings.stt_whisper_model

    logger.info("Reloading STT: %s -> %s", old_model, new_model)

    pipeline = _voice_bot.pipeline

    # Stop current STT
    try:
        await pipeline._stt.stop()
    except Exception:
        logger.exception("Error stopping old STT")

    # Create new STT provider
    from zoom_bot.stt_providers import FasterWhisperBatchSTT
    new_stt = FasterWhisperBatchSTT(
        model=new_model,
        silence_ms=int(settings.debounce_seconds * 1000),
    )

    # Wire and start
    new_stt.set_transcript_callback(pipeline._on_transcript)
    await new_stt.start()

    # Swap into pipeline
    pipeline._stt = new_stt

    # Persist
    settings.stt_whisper_model = new_model
    _save_runtime_overrides(stt_whisper_model=new_model)

    logger.info("STT reloaded: %s -> %s", old_model, new_model)
    return {"ok": True, "old_model": old_model, "new_model": new_model}


# ---------------------------------------------------------------------------
# Debug Audio Recordings
# ---------------------------------------------------------------------------

_RECORDINGS_DIR = Path("data/recordings")


@app.get("/debug/recordings")
async def list_recordings():
    """List all recordings in data/recordings/ with metadata."""
    if not _RECORDINGS_DIR.exists():
        return {"recordings": []}

    recordings = []
    # Find all .json metadata files
    for meta_path in sorted(_RECORDINGS_DIR.glob("*.json"), reverse=True):
        try:
            meta = json.loads(meta_path.read_text())
            # Add filename info for the UI
            base = meta_path.stem  # e.g. "20260216_233500_hello"
            meta["base"] = base
            meta["has_48k"] = (_RECORDINGS_DIR / f"{base}.48k.wav").exists()
            meta["has_32k"] = (_RECORDINGS_DIR / f"{base}.32k.wav").exists()
            recordings.append(meta)
        except Exception:
            continue

    # Also find orphan WAV files without metadata
    known_bases = {r["base"] for r in recordings}
    for wav_path in _RECORDINGS_DIR.glob("*.wav"):
        base = wav_path.name
        for suffix in (".48k.wav", ".32k.wav"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        if base not in known_bases:
            known_bases.add(base)
            recordings.append({
                "base": base,
                "timestamp": base[:15] if len(base) >= 15 else base,
                "transcript": "",
                "has_48k": (_RECORDINGS_DIR / f"{base}.48k.wav").exists(),
                "has_32k": (_RECORDINGS_DIR / f"{base}.32k.wav").exists(),
            })

    return {"recordings": recordings}


@app.get("/debug/recordings/{filename}")
async def get_recording(filename: str):
    """Serve a WAV file from the recordings directory."""
    # Sanitize filename to prevent directory traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")
    path = _RECORDINGS_DIR / filename
    if not path.exists() or not path.suffix == ".wav":
        raise HTTPException(404, "Recording not found")
    return FileResponse(str(path), media_type="audio/wav", filename=filename)


@app.post("/debug/retranscribe/{filename}")
async def retranscribe(filename: str, req: RetranscribeRequest | None = None):
    """Re-transcribe a WAV file using the remote STT service.

    Reads the 32kHz mono WAV, resamples to 16kHz, sends raw PCM to STT.
    """
    import struct as _struct
    import wave as _wave

    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")
    path = _RECORDINGS_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Recording not found")

    model = req.model if req else "medium.en"

    # Read the WAV file
    try:
        with _wave.open(str(path), "rb") as wf:
            n_channels = wf.getnchannels()
            sample_rate = wf.getframerate()
            pcm_data = wf.readframes(wf.getnframes())
    except Exception as exc:
        raise HTTPException(400, f"Cannot read WAV: {exc}")

    # If stereo, mix to mono
    if n_channels == 2:
        n_frames = len(pcm_data) // 4
        samples = _struct.unpack(f"<{n_frames * 2}h", pcm_data[: n_frames * 4])
        mono = []
        for i in range(n_frames):
            mono.append((samples[i * 2] + samples[i * 2 + 1]) // 2)
        pcm_data = _struct.pack(f"<{len(mono)}h", *mono)

    # Resample to 16kHz if needed
    if sample_rate != 16000:
        n_samples = len(pcm_data) // 2
        samples = _struct.unpack(f"<{n_samples}h", pcm_data)
        ratio = 16000 / sample_rate
        out_len = int(n_samples * ratio)
        result = []
        for i in range(out_len):
            src_idx = i / ratio
            idx = int(src_idx)
            frac = src_idx - idx
            if idx + 1 < len(samples):
                val = samples[idx] * (1 - frac) + samples[idx + 1] * frac
            else:
                val = samples[min(idx, len(samples) - 1)]
            result.append(max(-32768, min(32767, int(val))))
        pcm_16k = _struct.pack(f"<{len(result)}h", *result)
    else:
        pcm_16k = pcm_data

    # Send to STT service
    import httpx
    stt_url = getattr(settings, "stt_remote_url", "http://toddllm:8791")
    transcribe_url = f"{stt_url}/v1/transcribe"
    if model:
        transcribe_url += f"?model={model}"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                transcribe_url,
                content=pcm_16k,
                headers={"Content-Type": "application/octet-stream"},
            )
            if r.status_code == 200:
                data = r.json()
                transcript = data.get("text", data.get("transcript", ""))
                return {
                    "ok": True,
                    "model": model,
                    "transcript": transcript,
                    "filename": filename,
                    "pcm_bytes": len(pcm_16k),
                    "duration_s": round(len(pcm_16k) / (16000 * 2), 2),
                    "raw_response": data,
                }
            else:
                return {
                    "ok": False,
                    "error": f"STT returned {r.status_code}: {r.text[:200]}",
                    "model": model,
                }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "model": model}


@app.get("/debug/audio", response_class=HTMLResponse)
async def debug_audio_page():
    """HTML page to browse, play, and re-transcribe audio recordings."""
    return HTMLResponse(AUDIO_DEBUG_UI)


def main():
    """Entry point for the discord-bot command."""
    import uvicorn
    uvicorn.run(
        "discord_bot.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


# ---------------------------------------------------------------------------
# Control Panel Web UI
# ---------------------------------------------------------------------------

CONTROL_PANEL_UI = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Discord Bot — Control Panel</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    background: #1a1a2e; color: #e0e0e0;
    min-height: 100vh; padding: 20px;
  }
  .container { max-width: 860px; margin: 0 auto; }
  h1 { font-size: 20px; font-weight: 600; color: #7289da; margin-bottom: 4px; }
  .subtitle { font-size: 11px; color: #555; margin-bottom: 20px; }

  /* Status bar */
  .status-bar {
    display: flex; gap: 12px; flex-wrap: wrap;
    margin-bottom: 16px; padding: 12px;
    background: #16213e; border: 1px solid #0f3460;
    border-radius: 8px; align-items: center;
  }
  .status-dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: #ff5252; flex-shrink: 0;
  }
  .status-dot.on { background: #00e676; }
  .status-label { font-size: 12px; font-weight: 500; }
  .status-detail { font-size: 10px; color: #888; }
  .status-spacer { flex: 1; }
  .status-uptime { font-size: 11px; color: #7289da; font-variant-numeric: tabular-nums; }

  /* Grid */
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 700px) { .grid { grid-template-columns: 1fr; } }

  .card {
    background: #16213e; border: 1px solid #0f3460;
    border-radius: 8px; padding: 14px;
  }
  .card.full { grid-column: 1 / -1; }
  .card-title {
    font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 1px; color: #0f3460; margin-bottom: 10px;
  }

  /* Form elements */
  .form-row { margin-bottom: 10px; }
  .form-row:last-child { margin-bottom: 0; }
  .form-label {
    display: block; font-size: 10px; color: #888;
    text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 3px;
  }
  select, input[type=text], input[type=number], textarea {
    width: 100%; font-size: 12px; padding: 7px 9px;
    background: #1a1a2e; color: #e0e0e0;
    border: 1px solid #0f3460; border-radius: 4px;
    outline: none; font-family: inherit;
  }
  select:focus, input:focus, textarea:focus { border-color: #7289da; }
  textarea { resize: vertical; min-height: 70px; }
  .input-row { display: flex; gap: 6px; }
  .input-row select, .input-row input { flex: 1; }

  /* Buttons */
  .btn {
    font-size: 11px; padding: 6px 14px;
    border: 1px solid #0f3460; border-radius: 4px;
    background: #1a1a2e; color: #e0e0e0;
    cursor: pointer; transition: all 0.15s; white-space: nowrap;
  }
  .btn:hover { background: #0f3460; border-color: #7289da; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-apply {
    border-color: #7289da; color: #7289da;
    background: rgba(114, 137, 218, 0.08);
  }
  .btn-apply:hover { background: rgba(114, 137, 218, 0.2); }
  .btn-danger { border-color: #ff5252; color: #ff5252; }
  .btn-danger:hover { background: rgba(255, 82, 82, 0.15); }
  .btn-success { border-color: #00e676; color: #00e676; }
  .btn-success:hover { background: rgba(0, 230, 118, 0.15); }

  /* Inline slider */
  .slider-row { display: flex; align-items: center; gap: 8px; }
  .slider-row input[type=range] { flex: 1; accent-color: #7289da; }
  .slider-val { font-size: 11px; color: #7289da; min-width: 32px; text-align: right; font-variant-numeric: tabular-nums; }

  /* Toast / flash message */
  .toast {
    font-size: 11px; padding: 8px 12px; border-radius: 4px;
    text-align: center; margin-top: 8px; min-height: 14px;
  }
  .toast.ok { color: #00e676; background: rgba(0,230,118,0.08); }
  .toast.err { color: #ff5252; background: rgba(255,82,82,0.08); }
  .toast.info { color: #7289da; background: rgba(114,137,218,0.08); }

  /* Latency test */
  .latency-row {
    display: flex; align-items: center; gap: 6px;
    padding: 5px 7px; border-radius: 4px; margin-bottom: 3px;
    font-size: 11px; background: rgba(15,52,96,0.2);
  }
  .lat-voice { font-weight: 500; min-width: 90px; }
  .lat-ms { min-width: 55px; font-variant-numeric: tabular-nums; }
  .lat-ms.fast { color: #00e676; }
  .lat-ms.mid { color: #ffc107; }
  .lat-ms.slow { color: #ff5252; }
  .lat-bar { flex: 1; height: 5px; background: #1a1a2e; border-radius: 3px; overflow: hidden; }
  .lat-bar-fill { height: 100%; border-radius: 3px; }
  .lat-play { cursor: pointer; opacity: 0.5; }
  .lat-play:hover { opacity: 1; }
  .lat-size { font-size: 10px; color: #555; min-width: 40px; }

  /* Live events */
  .evt { padding: 4px 8px; border-radius: 3px; margin-bottom: 3px; display: flex; gap: 8px; align-items: baseline; }
  .evt-time { color: #555; font-size: 10px; min-width: 55px; font-variant-numeric: tabular-nums; }
  .evt-type { font-size: 9px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; min-width: 65px; }
  .evt-type.transcript { color: #2196f3; }
  .evt-type.llm { color: #00e676; }
  .evt-type.tts { color: #ffc107; }
  .evt-text { flex: 1; word-break: break-word; }

  /* Conversation */
  .msg { padding: 6px 10px; border-radius: 6px; margin-bottom: 4px; }
  .msg.system { background: rgba(114,137,218,0.08); color: #7289da; font-size: 10px; font-style: italic; }
  .msg.user { background: rgba(33,150,243,0.08); border-left: 3px solid #2196f3; }
  .msg.assistant { background: rgba(0,230,118,0.08); border-left: 3px solid #00e676; }
  .msg-role { font-size: 9px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 2px; }
  .msg-role.user { color: #2196f3; }
  .msg-role.assistant { color: #00e676; }
  .msg-role.system { color: #7289da; }

  /* Config summary */
  .cfg-row { display: flex; gap: 12px; margin-bottom: 4px; flex-wrap: wrap; }
  .cfg-item { display: flex; gap: 4px; }
  .cfg-label { color: #555; }
  .cfg-val { color: #e0e0e0; font-weight: 500; }
</style>
</head>
<body>
<div class="container">
  <h1>Discord Bot Control Panel</h1>
  <div class="subtitle">Manage voice, model, and pipeline settings — changes take effect immediately</div>

  <!-- Status Bar -->
  <div class="status-bar" id="status-bar">
    <div class="status-dot" id="status-dot"></div>
    <span class="status-label" id="status-label">checking...</span>
    <span class="status-detail" id="status-detail"></span>
    <span class="status-spacer"></span>
    <span class="status-uptime" id="status-uptime"></span>
    <select id="sel-channel" style="font-size:11px;padding:4px 6px;background:#1a1a2e;color:#e0e0e0;border:1px solid #0f3460;border-radius:4px;max-width:200px">
      <option value="">select channel...</option>
    </select>
    <button class="btn btn-success" id="btn-join">Join</button>
    <button class="btn btn-danger" id="btn-leave" disabled>Leave</button>
    <span style="border-left:1px solid #0f3460;height:20px;margin:0 4px;"></span>
    <button class="btn" id="btn-mute" title="Mute/Unmute bot responses">Mute</button>
    <select id="sel-mode" style="font-size:11px;padding:4px 6px;background:#1a1a2e;color:#e0e0e0;border:1px solid #0f3460;border-radius:4px;" title="Response mode">
      <option value="auto">Auto</option>
      <option value="keyword">Keyword</option>
      <option value="manual">Manual</option>
    </select>
    <button class="btn btn-danger" id="btn-cancel" title="Cancel in-flight response">Cancel</button>
  </div>

  <div class="grid">
    <!-- TTS Voice -->
    <div class="card">
      <div class="card-title">TTS Voice</div>
      <div class="form-row">
        <label class="form-label">Voice</label>
        <div class="input-row">
          <select id="sel-voice"><option>loading...</option></select>
          <button class="btn btn-apply" id="btn-voice-apply">Apply</button>
        </div>
      </div>
      <div class="form-row">
        <label class="form-label">Test Text</label>
        <div class="input-row">
          <input type="text" id="tts-test-text" value="Hello, this is a test of my voice." />
          <button class="btn" id="btn-tts-test">Speak</button>
        </div>
      </div>
      <div class="toast" id="voice-toast"></div>
    </div>

    <!-- LLM Model -->
    <div class="card">
      <div class="card-title">LLM Model</div>
      <div class="form-row">
        <label class="form-label">Ollama Model</label>
        <div class="input-row">
          <select id="sel-model"><option>loading...</option></select>
          <button class="btn btn-apply" id="btn-model-apply">Apply</button>
        </div>
      </div>
      <div class="form-row">
        <label class="form-label">Temperature</label>
        <div class="slider-row">
          <input type="range" id="rng-temp" min="0" max="2" step="0.1" value="0.8" />
          <span class="slider-val" id="lbl-temp">0.8</span>
        </div>
      </div>
      <div class="form-row">
        <label class="form-label">Max Tokens</label>
        <div class="slider-row">
          <input type="range" id="rng-tokens" min="20" max="500" step="10" value="120" />
          <span class="slider-val" id="lbl-tokens">120</span>
        </div>
      </div>
      <div class="toast" id="model-toast"></div>
    </div>

    <!-- System Prompt -->
    <div class="card full">
      <div class="card-title">System Prompt</div>
      <div class="form-row">
        <textarea id="txt-prompt" rows="3"></textarea>
      </div>
      <div style="display:flex; gap:8px; margin-top:6px;">
        <button class="btn btn-apply" id="btn-prompt-apply">Save Prompt</button>
        <span class="toast" id="prompt-toast" style="margin:0; line-height:28px;"></span>
      </div>
    </div>

    <!-- Puppet Master -->
    <div class="card full">
      <div class="card-title">Puppet Master</div>
      <div class="form-row">
        <label class="form-label">Speak (text &rarr; TTS &rarr; voice channel)</label>
        <div class="input-row">
          <input type="text" id="txt-speak" placeholder="Type something for the bot to say..." />
          <button class="btn btn-apply" id="btn-speak">Send</button>
        </div>
      </div>
      <div class="form-row">
        <label class="form-label">Nudge (one-shot hint for next response)</label>
        <div class="input-row">
          <input type="text" id="txt-nudge" placeholder="e.g. Be more enthusiastic, ask about their weekend..." />
          <button class="btn" id="btn-nudge">Nudge</button>
        </div>
      </div>
      <div class="form-row">
        <label class="form-label">Inject Context (add to conversation silently)</label>
        <div class="input-row" style="flex-direction:column;gap:4px;">
          <textarea id="txt-inject" rows="2" placeholder="Context text to inject into conversation..."></textarea>
          <div style="display:flex;gap:6px;">
            <select id="sel-inject-role" style="max-width:120px;">
              <option value="system">system</option>
              <option value="user">user</option>
            </select>
            <button class="btn" id="btn-inject">Inject</button>
          </div>
        </div>
      </div>
      <div class="toast" id="puppet-toast"></div>
    </div>

    <!-- Response Latency -->
    <div class="card">
      <div class="card-title">Response Latency (last)</div>
      <div id="latency-display" style="font-size:11px; color:#888;">
        <div style="display:flex;gap:12px;flex-wrap:wrap;" id="latency-items">
          <span>Waiting for first response...</span>
        </div>
      </div>
    </div>

    <!-- Latency Test -->
    <div class="card full">
      <div class="card-title">Voice Latency Benchmark</div>
      <div class="form-row">
        <div class="input-row">
          <input type="text" id="bench-text" value="The quick brown fox jumps over the lazy dog." />
          <select id="bench-sel" style="max-width:160px"><option value="__all__">All voices</option></select>
          <button class="btn" id="btn-bench">Run</button>
          <button class="btn" id="btn-bench-all">Benchmark All</button>
        </div>
      </div>
      <div id="bench-results" style="margin-top:8px;">
        <div style="color:#444; font-size:11px; font-style:italic; text-align:center; padding:12px;">Run a test to see results</div>
      </div>
    </div>

    <!-- Live Activity & Conversation -->
    <div class="card full">
      <div class="card-title">Live Activity</div>
      <div id="live-events" style="max-height:250px; overflow-y:auto; font-size:11px;">
        <div style="color:#444; font-style:italic; text-align:center; padding:12px;">Waiting for activity...</div>
      </div>
    </div>

    <div class="card full">
      <div class="card-title">Conversation</div>
      <div id="conversation" style="max-height:350px; overflow-y:auto; font-size:12px;">
        <div style="color:#444; font-style:italic; text-align:center; padding:12px;">No conversation yet</div>
      </div>
    </div>

    <!-- Current Settings Summary -->
    <div class="card full">
      <div class="card-title">Current Configuration</div>
      <div id="config-summary" style="font-size:11px; color:#888;">Loading...</div>
    </div>
  </div>
</div>

<script>
const API = '';
const TTS_URL = '""" + "http://toddllm:8790" + """';

// --- State ---
let benchData = [];
let playingAudio = null;

// --- Init ---
async function init() {
  loadStatus();
  loadSettings();
  loadVoices();
  loadModels();

  setInterval(loadStatus, 5000);
  setInterval(loadVoices, 30000);

  document.getElementById('btn-voice-apply').onclick = applyVoice;
  document.getElementById('btn-tts-test').onclick = testTTS;
  document.getElementById('btn-model-apply').onclick = applyModel;
  document.getElementById('btn-prompt-apply').onclick = applyPrompt;
  document.getElementById('btn-join').onclick = joinChannel;
  document.getElementById('btn-leave').onclick = leaveChannel;
  document.getElementById('btn-bench').onclick = runBench;
  document.getElementById('btn-bench-all').onclick = () => {
    document.getElementById('bench-sel').value = '__all__';
    runBench();
  };
  loadGuilds();
  loadConversation();
  loadConfig();
  pollEvents();
  loadPipelineState();

  setInterval(loadConversation, 5000);
  setInterval(loadConfig, 10000);
  setInterval(loadPipelineState, 2000);

  // Slider live labels
  document.getElementById('rng-temp').oninput = (e) => {
    document.getElementById('lbl-temp').textContent = parseFloat(e.target.value).toFixed(1);
  };
  document.getElementById('rng-tokens').oninput = (e) => {
    document.getElementById('lbl-tokens').textContent = e.target.value;
  };
  // Puppet master buttons
  document.getElementById('btn-speak').onclick = doSpeak;
  document.getElementById('btn-nudge').onclick = doNudge;
  document.getElementById('btn-inject').onclick = doInject;
  document.getElementById('btn-mute').onclick = toggleMute;
  document.getElementById('btn-cancel').onclick = doCancel;
  document.getElementById('sel-mode').onchange = changeResponseMode;
}

// --- Status ---
async function loadStatus() {
  try {
    const r = await fetch(API + '/status');
    const d = await r.json();
    const dot = document.getElementById('status-dot');
    const label = document.getElementById('status-label');
    const detail = document.getElementById('status-detail');
    const uptime = document.getElementById('status-uptime');
    const joinBtn = document.getElementById('btn-join');
    const leaveBtn = document.getElementById('btn-leave');
    if (d.connected) {
      dot.className = 'status-dot on';
      label.textContent = 'Connected';
      detail.textContent = 'guild: ' + d.guild_id + ' | stt: ' + d.stt_provider + ' | llm: ' + d.llm_provider + ' | tts: ' + d.tts_provider;
      if (d.uptime_seconds) {
        const m = Math.floor(d.uptime_seconds / 60);
        const s = Math.floor(d.uptime_seconds % 60);
        uptime.textContent = m + 'm ' + s + 's';
      }
      if (d.active_speakers != null) {
        uptime.textContent += ' | ' + d.active_speakers + ' speaker(s)';
      }
      joinBtn.disabled = true;
      leaveBtn.disabled = false;
    } else {
      dot.className = 'status-dot';
      label.textContent = 'Not in voice channel';
      detail.textContent = d.stt_provider ? ('stt: ' + d.stt_provider + ' | llm: ' + d.llm_provider + ' | tts: ' + d.tts_provider) : '';
      uptime.textContent = '';
      joinBtn.disabled = false;
      leaveBtn.disabled = true;
    }
  } catch (e) {
    document.getElementById('status-label').textContent = 'Service error';
  }
}

// --- Guilds / Join / Leave ---
async function loadGuilds() {
  const sel = document.getElementById('sel-channel');
  try {
    const r = await fetch(API + '/guilds');
    const d = await r.json();
    sel.innerHTML = '<option value="">select channel...</option>';
    (d.guilds || []).forEach(g => {
      const grp = document.createElement('optgroup');
      grp.label = g.name;
      (g.channels || []).forEach(ch => {
        const o = document.createElement('option');
        o.value = g.id + ':' + ch.id;
        const who = ch.members.length > 0 ? ' (' + ch.members.join(', ') + ')' : '';
        o.textContent = ch.name + who;
        grp.appendChild(o);
      });
      sel.appendChild(grp);
    });
  } catch (e) {
    sel.innerHTML = '<option value="">error loading guilds</option>';
  }
}

async function joinChannel() {
  const sel = document.getElementById('sel-channel');
  const val = sel.value;
  if (!val) { toast('voice-toast', 'Select a channel first', 'err'); return; }
  const [guildId, channelId] = val.split(':');
  const btn = document.getElementById('btn-join');
  btn.disabled = true; btn.textContent = 'Joining...';
  try {
    const r = await fetch(API + '/join', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ guild_id: guildId, channel_id: channelId }),
    });
    if (r.ok) {
      loadStatus();
      loadGuilds();
    } else {
      const d = await r.json();
      toast('voice-toast', d.detail || 'Join failed', 'err');
    }
  } catch (e) { toast('voice-toast', 'Error: ' + e.message, 'err'); }
  btn.textContent = 'Join'; btn.disabled = false;
}

async function leaveChannel() {
  const btn = document.getElementById('btn-leave');
  btn.disabled = true; btn.textContent = 'Leaving...';
  try {
    await fetch(API + '/leave', { method: 'POST' });
    loadStatus();
  } catch (e) { toast('voice-toast', 'Error: ' + e.message, 'err'); }
  btn.textContent = 'Leave'; btn.disabled = false;
}

// --- Live Events ---
let lastEventTs = 0;
async function pollEvents() {
  try {
    const r = await fetch(API + '/events?since=' + lastEventTs);
    const d = await r.json();
    const events = d.events || [];
    if (events.length > 0) {
      const el = document.getElementById('live-events');
      if (el.querySelector('div[style]')) el.innerHTML = '';
      events.forEach(evt => {
        lastEventTs = Math.max(lastEventTs, evt.ts || 0);
        updateLatencyDisplay(evt);
        const div = document.createElement('div');
        div.className = 'evt';
        const time = new Date(evt.ts * 1000).toLocaleTimeString();
        let typeCls = '';
        let typeLabel = evt.type || '?';
        if (evt.type === 'transcript') { typeCls = 'transcript'; typeLabel = 'STT'; }
        else if (evt.type === 'llm_response') { typeCls = 'llm'; typeLabel = 'LLM'; }
        else if (evt.type === 'tts_audio') { typeCls = 'tts'; typeLabel = 'TTS'; }
        else if (evt.type === 'echo_suppressed') { typeCls = ''; typeLabel = 'ECHO'; }
        else if (evt.type === 'cancelled') { typeCls = ''; typeLabel = 'CANCEL'; }
        const text = evt.text || evt.hint || (evt.duration_s ? evt.duration_s + 's audio' : JSON.stringify(evt));
        div.innerHTML = '<span class="evt-time">' + time + '</span>' +
          '<span class="evt-type ' + typeCls + '">' + typeLabel + '</span>' +
          '<span class="evt-text">' + esc(text) + '</span>';
        el.appendChild(div);
        el.scrollTop = el.scrollHeight;
      });
    }
  } catch (e) {}
  setTimeout(pollEvents, 1000);
}

// --- Conversation ---
async function loadConversation() {
  try {
    const r = await fetch(API + '/conversation');
    const d = await r.json();
    const msgs = d.conversation || [];
    const el = document.getElementById('conversation');
    if (msgs.length <= 1) {
      el.innerHTML = '<div style="color:#444; font-style:italic; text-align:center; padding:12px;">No conversation yet</div>';
      return;
    }
    el.innerHTML = msgs.map(m => {
      const role = m.role || 'system';
      const cls = role === 'user' ? 'user' : role === 'assistant' ? 'assistant' : 'system';
      return '<div class="msg ' + cls + '"><div class="msg-role ' + cls + '">' + role + '</div>' + esc(m.content || '') + '</div>';
    }).join('');
    el.scrollTop = el.scrollHeight;
  } catch (e) {}
}

// --- Config Summary ---
async function loadConfig() {
  try {
    const r = await fetch(API + '/settings');
    const d = await r.json();
    const el = document.getElementById('config-summary');
    el.innerHTML =
      '<div class="cfg-row">' +
        '<div class="cfg-item"><span class="cfg-label">Voice:</span> <span class="cfg-val">' + esc(d.tts_voice) + '</span></div>' +
        '<div class="cfg-item"><span class="cfg-label">TTS:</span> <span class="cfg-val">' + esc(d.tts_url) + '</span></div>' +
        '<div class="cfg-item"><span class="cfg-label">Model:</span> <span class="cfg-val">' + esc(d.llm_model) + '</span></div>' +
        '<div class="cfg-item"><span class="cfg-label">LLM:</span> <span class="cfg-val">' + esc(d.llm_url) + '</span></div>' +
      '</div>' +
      '<div class="cfg-row">' +
        '<div class="cfg-item"><span class="cfg-label">Temp:</span> <span class="cfg-val">' + d.llm_temperature + '</span></div>' +
        '<div class="cfg-item"><span class="cfg-label">Max Tokens:</span> <span class="cfg-val">' + d.llm_max_tokens + '</span></div>' +
        '<div class="cfg-item"><span class="cfg-label">Debounce:</span> <span class="cfg-val">' + d.debounce_seconds + 's</span></div>' +
      '</div>';
  } catch (e) {}
}

// --- Settings ---
async function loadSettings() {
  try {
    const r = await fetch(API + '/settings');
    const d = await r.json();
    document.getElementById('rng-temp').value = d.llm_temperature;
    document.getElementById('lbl-temp').textContent = d.llm_temperature.toFixed(1);
    document.getElementById('rng-tokens').value = d.llm_max_tokens;
    document.getElementById('lbl-tokens').textContent = d.llm_max_tokens;
    document.getElementById('txt-prompt').value = d.system_prompt;
  } catch (e) {}
}

// --- Voices ---
async function loadVoices() {
  const sel = document.getElementById('sel-voice');
  const benchSel = document.getElementById('bench-sel');
  try {
    const r = await fetch(API + '/voice');
    const d = await r.json();
    const voices = d.available_voices || [];

    function populate(el, current, addAll) {
      el.innerHTML = '';
      if (addAll) {
        const o = document.createElement('option');
        o.value = '__all__'; o.textContent = 'All voices';
        el.appendChild(o);
      }
      const cloned = voices.filter(v => v.voice_type === 'cloned');
      const builtin = voices.filter(v => v.voice_type !== 'cloned');
      if (cloned.length) {
        const g = document.createElement('optgroup'); g.label = 'Cloned';
        cloned.forEach(v => {
          const o = document.createElement('option');
          o.value = v.name; o.textContent = v.display_name + (v.is_owner ? ' (owner)' : '');
          if (v.name === current) o.selected = true;
          g.appendChild(o);
        });
        el.appendChild(g);
      }
      if (builtin.length) {
        const g = document.createElement('optgroup'); g.label = 'Builtin';
        builtin.forEach(v => {
          const o = document.createElement('option');
          o.value = v.name; o.textContent = v.display_name;
          if (v.name === current) o.selected = true;
          g.appendChild(o);
        });
        el.appendChild(g);
      }
    }
    populate(sel, d.voice, false);
    populate(benchSel, null, true);
  } catch (e) {
    sel.innerHTML = '<option>error loading</option>';
  }
}

// --- Models ---
async function loadModels() {
  const sel = document.getElementById('sel-model');
  try {
    const [modelsResp, settingsResp] = await Promise.all([
      fetch(API + '/ollama/models'),
      fetch(API + '/settings'),
    ]);
    const models = (await modelsResp.json()).models || [];
    const current = (await settingsResp.json()).llm_model || '';
    sel.innerHTML = '';
    models.forEach(m => {
      const o = document.createElement('option');
      o.value = m; o.textContent = m;
      if (m === current) o.selected = true;
      sel.appendChild(o);
    });
    if (!models.includes(current) && current) {
      const o = document.createElement('option');
      o.value = current; o.textContent = current + ' (current)';
      o.selected = true;
      sel.insertBefore(o, sel.firstChild);
    }
  } catch (e) {
    sel.innerHTML = '<option>error</option>';
  }
}

// --- Apply ---
async function applyVoice() {
  const voice = document.getElementById('sel-voice').value;
  if (!voice) return;
  try {
    const r = await fetch(API + '/settings', {
      method: 'PUT', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ tts_voice: voice }),
    });
    if (!r.ok) { const e = await r.json(); toast('voice-toast', e.detail || 'Error ' + r.status, 'err'); return; }
    const d = await r.json();
    toast('voice-toast', 'Voice set to: ' + d.tts_voice, 'ok');
    loadConfig();
  } catch (e) { toast('voice-toast', 'Error: ' + e.message, 'err'); }
}

async function applyModel() {
  const model = document.getElementById('sel-model').value;
  const temp = parseFloat(document.getElementById('rng-temp').value);
  const tokens = parseInt(document.getElementById('rng-tokens').value);
  if (!model || model === 'loading...') { toast('model-toast', 'Select a model first', 'err'); return; }
  try {
    const r = await fetch(API + '/settings', {
      method: 'PUT', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ llm_model: model, llm_temperature: temp, llm_max_tokens: tokens }),
    });
    if (!r.ok) { const e = await r.json(); toast('model-toast', e.detail || 'Error ' + r.status, 'err'); return; }
    const d = await r.json();
    toast('model-toast', 'Applied: ' + d.llm_model + ' | temp=' + d.llm_temperature + ' | tokens=' + d.llm_max_tokens, 'ok');
    loadConfig();
  } catch (e) { toast('model-toast', 'Error: ' + e.message, 'err'); }
}

async function applyPrompt() {
  const prompt = document.getElementById('txt-prompt').value;
  try {
    const r = await fetch(API + '/settings', {
      method: 'PUT', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ system_prompt: prompt }),
    });
    if (!r.ok) { const e = await r.json(); toast('prompt-toast', e.detail || 'Error', 'err'); return; }
    toast('prompt-toast', 'Saved', 'ok');
    loadConfig();
  } catch (e) { toast('prompt-toast', 'Error: ' + e.message, 'err'); }
}

// --- TTS Test ---
async function testTTS() {
  const voice = document.getElementById('sel-voice').value;
  const text = document.getElementById('tts-test-text').value.trim();
  if (!voice || !text) return;
  const btn = document.getElementById('btn-tts-test');
  btn.disabled = true; btn.textContent = '...';
  if (playingAudio) { playingAudio.pause(); playingAudio = null; }
  try {
    const r = await fetch(TTS_URL + '/v1/synthesize', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ text, voice, save_audio: false }),
    });
    const d = await r.json();
    if (d.audio_base64) {
      playingAudio = new Audio('data:audio/wav;base64,' + d.audio_base64);
      playingAudio.onended = () => { btn.textContent = 'Speak'; btn.disabled = false; playingAudio = null; };
      btn.textContent = 'Playing...';
      playingAudio.play();
      return;
    }
  } catch (e) { toast('voice-toast', 'Error: ' + e.message, 'err'); }
  btn.textContent = 'Speak'; btn.disabled = false;
}

// --- Benchmark ---
async function runBench() {
  const text = document.getElementById('bench-text').value.trim();
  if (!text) return;
  const voiceSel = document.getElementById('bench-sel').value;
  const btnRun = document.getElementById('btn-bench');
  const btnAll = document.getElementById('btn-bench-all');
  btnRun.disabled = true; btnAll.disabled = true;

  let voices = [];
  if (voiceSel === '__all__') {
    const opts = document.getElementById('bench-sel').options;
    for (let i = 1; i < opts.length; i++) voices.push(opts[i].value);
  } else {
    voices = [voiceSel];
  }

  benchData = [];
  for (let i = 0; i < voices.length; i++) {
    btnRun.textContent = (i+1) + '/' + voices.length;
    try {
      const t0 = performance.now();
      const r = await fetch(TTS_URL + '/v1/synthesize', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ text, voice: voices[i], save_audio: false }),
      });
      const d = await r.json();
      const ms = performance.now() - t0;
      const kb = d.audio_base64 ? Math.round(d.audio_base64.length * 3/4/1024) : 0;
      benchData.push({ voice: voices[i], ms, kb, b64: d.audio_base64, provider: d.provider||'' });
    } catch (e) {
      benchData.push({ voice: voices[i], ms: -1, kb: 0, b64: null, err: e.message });
    }
    renderBench();
  }
  btnRun.textContent = 'Run'; btnRun.disabled = false; btnAll.disabled = false;
}

function renderBench() {
  const el = document.getElementById('bench-results');
  if (!benchData.length) { el.innerHTML = '<div style="color:#444;font-size:11px;text-align:center;padding:12px">No results</div>'; return; }
  const sorted = [...benchData].sort((a,b) => (a.ms<0?9e9:a.ms) - (b.ms<0?9e9:b.ms));
  const maxMs = Math.max(...sorted.filter(r=>r.ms>0).map(r=>r.ms), 1);
  el.innerHTML = sorted.map((r, i) => {
    if (r.ms < 0) return '<div class="latency-row"><span class="lat-voice">' + esc(r.voice) + '</span><span class="lat-ms slow">error</span></div>';
    const ms = Math.round(r.ms);
    const cls = ms > 5000 ? 'slow' : ms > 2000 ? 'mid' : 'fast';
    const color = ms > 5000 ? '#ff5252' : ms > 2000 ? '#ffc107' : '#00e676';
    const pct = Math.round(r.ms / maxMs * 100);
    const tag = r.provider.includes('clone') ? ' <span style="font-size:9px;color:#7289da;background:rgba(114,137,218,0.15);padding:1px 5px;border-radius:2px">clone</span>' : '';
    return '<div class="latency-row">' +
      '<span class="lat-voice">' + esc(r.voice) + tag + '</span>' +
      '<span class="lat-ms ' + cls + '">' + ms + 'ms</span>' +
      '<div class="lat-bar"><div class="lat-bar-fill" style="width:'+pct+'%;background:'+color+'"></div></div>' +
      '<span class="lat-size">' + r.kb + 'KB</span>' +
      (r.b64 ? '<span class="lat-play" onclick="playBench('+i+')">&#9654;</span>' : '') +
      '</div>';
  }).join('');
}

function playBench(idx) {
  if (playingAudio) { playingAudio.pause(); playingAudio = null; }
  const sorted = [...benchData].sort((a,b) => (a.ms<0?9e9:a.ms) - (b.ms<0?9e9:b.ms));
  const r = sorted[idx];
  if (!r || !r.b64) return;
  playingAudio = new Audio('data:audio/wav;base64,' + r.b64);
  playingAudio.onended = () => { playingAudio = null; };
  playingAudio.play();
}

// --- Pipeline State ---
async function loadPipelineState() {
  try {
    const r = await fetch(API + '/pipeline-state');
    const d = await r.json();
    const muteBtn = document.getElementById('btn-mute');
    const modeSel = document.getElementById('sel-mode');
    if (d.muted) {
      muteBtn.textContent = 'Unmute';
      muteBtn.className = 'btn btn-success';
    } else {
      muteBtn.textContent = 'Mute';
      muteBtn.className = 'btn';
    }
    modeSel.value = d.response_mode || 'auto';
  } catch (e) {}
}

// --- Puppet Master ---
async function doSpeak() {
  const text = document.getElementById('txt-speak').value.trim();
  if (!text) return;
  const btn = document.getElementById('btn-speak');
  btn.disabled = true; btn.textContent = '...';
  try {
    const r = await fetch(API + '/speak', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ text }),
    });
    if (r.ok) {
      toast('puppet-toast', 'Speaking: ' + text.substring(0, 60), 'ok');
      document.getElementById('txt-speak').value = '';
    } else {
      const d = await r.json();
      toast('puppet-toast', d.detail || 'Error', 'err');
    }
  } catch (e) { toast('puppet-toast', 'Error: ' + e.message, 'err'); }
  btn.disabled = false; btn.textContent = 'Send';
}

async function doNudge() {
  const hint = document.getElementById('txt-nudge').value.trim();
  if (!hint) return;
  try {
    const r = await fetch(API + '/nudge', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ hint }),
    });
    if (r.ok) {
      toast('puppet-toast', 'Nudge queued', 'ok');
      document.getElementById('txt-nudge').value = '';
    } else {
      const d = await r.json();
      toast('puppet-toast', d.detail || 'Error', 'err');
    }
  } catch (e) { toast('puppet-toast', 'Error: ' + e.message, 'err'); }
}

async function doInject() {
  const text = document.getElementById('txt-inject').value.trim();
  const role = document.getElementById('sel-inject-role').value;
  if (!text) return;
  try {
    const r = await fetch(API + '/inject', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ text, role }),
    });
    if (r.ok) {
      toast('puppet-toast', 'Injected ' + role + ' context', 'ok');
      document.getElementById('txt-inject').value = '';
      loadConversation();
    } else {
      const d = await r.json();
      toast('puppet-toast', d.detail || 'Error', 'err');
    }
  } catch (e) { toast('puppet-toast', 'Error: ' + e.message, 'err'); }
}

async function toggleMute() {
  const btn = document.getElementById('btn-mute');
  const isMuted = btn.textContent === 'Unmute';
  const endpoint = isMuted ? '/unmute' : '/mute';
  try {
    const r = await fetch(API + endpoint, { method: 'POST' });
    if (r.ok) {
      loadPipelineState();
    }
  } catch (e) {}
}

async function doCancel() {
  try {
    await fetch(API + '/cancel', { method: 'POST' });
    toast('puppet-toast', 'Cancelled', 'info');
  } catch (e) {}
}

async function changeResponseMode() {
  const mode = document.getElementById('sel-mode').value;
  let keyword = '';
  if (mode === 'keyword') {
    keyword = prompt('Enter trigger keyword:', 'hey bot') || 'hey bot';
  }
  try {
    await fetch(API + '/response-mode', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ mode, keyword }),
    });
  } catch (e) {}
}

// --- Response Latency (from events) ---
function updateLatencyDisplay(evt) {
  if (evt.type !== 'tts_audio') return;
  const el = document.getElementById('latency-items');
  const llm = evt.llm_s != null ? evt.llm_s + 's' : '?';
  const tts = evt.synth_s != null ? evt.synth_s + 's' : '?';
  const total = evt.total_s != null ? evt.total_s + 's' : '?';
  const dur = evt.duration_s != null ? evt.duration_s + 's' : '?';
  el.innerHTML =
    '<div class="cfg-item"><span class="cfg-label">LLM:</span> <span class="cfg-val">' + llm + '</span></div>' +
    '<div class="cfg-item"><span class="cfg-label">TTS:</span> <span class="cfg-val">' + tts + '</span></div>' +
    '<div class="cfg-item"><span class="cfg-label">Total:</span> <span class="cfg-val">' + total + '</span></div>' +
    '<div class="cfg-item"><span class="cfg-label">Audio:</span> <span class="cfg-val">' + dur + '</span></div>';
}

// --- Helpers ---
function toast(id, msg, type) {
  const el = document.getElementById(id);
  el.textContent = msg; el.className = 'toast ' + (type||'');
  if (type === 'ok') setTimeout(() => { if (el.textContent === msg) el.textContent = ''; }, 4000);
}
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

init();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Audio Debug UI
# ---------------------------------------------------------------------------

AUDIO_DEBUG_UI = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Audio Debug — Recordings</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    background: #1a1a2e; color: #e0e0e0;
    min-height: 100vh; padding: 20px;
  }
  .container { max-width: 960px; margin: 0 auto; }
  h1 { font-size: 20px; font-weight: 600; color: #7289da; margin-bottom: 4px; }
  .subtitle { font-size: 11px; color: #555; margin-bottom: 20px; }
  .nav-link { font-size: 11px; color: #7289da; text-decoration: none; }
  .nav-link:hover { text-decoration: underline; }
  .header-row { display: flex; align-items: baseline; gap: 16px; margin-bottom: 16px; }

  .recording {
    background: #16213e; border: 1px solid #0f3460;
    border-radius: 8px; padding: 14px; margin-bottom: 12px;
  }
  .rec-header {
    display: flex; gap: 12px; align-items: baseline; margin-bottom: 8px; flex-wrap: wrap;
  }
  .rec-time { font-size: 12px; font-weight: 600; color: #7289da; font-variant-numeric: tabular-nums; }
  .rec-transcript { font-size: 13px; color: #e0e0e0; flex: 1; word-break: break-word; }
  .rec-duration { font-size: 10px; color: #555; }

  .audio-row {
    display: flex; gap: 12px; align-items: center; margin-bottom: 6px; flex-wrap: wrap;
  }
  .audio-label {
    font-size: 10px; color: #888; text-transform: uppercase;
    letter-spacing: 0.5px; min-width: 65px;
  }
  audio { height: 28px; flex: 1; min-width: 180px; }
  audio::-webkit-media-controls-panel { background: #1a1a2e; }

  .retranscribe-row {
    display: flex; gap: 8px; align-items: center; margin-top: 8px; flex-wrap: wrap;
  }
  .btn {
    font-size: 11px; padding: 5px 12px;
    border: 1px solid #0f3460; border-radius: 4px;
    background: #1a1a2e; color: #e0e0e0;
    cursor: pointer; transition: all 0.15s; white-space: nowrap;
  }
  .btn:hover { background: #0f3460; border-color: #7289da; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-primary { border-color: #7289da; color: #7289da; background: rgba(114,137,218,0.08); }
  .btn-primary:hover { background: rgba(114,137,218,0.2); }
  .btn-refresh { border-color: #00e676; color: #00e676; }
  .btn-refresh:hover { background: rgba(0,230,118,0.15); }

  .retranscript-result {
    margin-top: 6px; padding: 6px 10px; border-radius: 4px;
    font-size: 12px; background: rgba(114,137,218,0.06);
    border-left: 3px solid #7289da;
  }
  .retranscript-result .model-tag {
    font-size: 9px; font-weight: 600; text-transform: uppercase;
    color: #7289da; letter-spacing: 0.5px;
  }
  .retranscript-result .result-text { margin-top: 2px; }
  .retranscript-result.error { border-left-color: #ff5252; }
  .retranscript-result.error .model-tag { color: #ff5252; }

  .empty-state {
    text-align: center; padding: 40px; color: #444;
    font-style: italic; font-size: 13px;
  }

  .status-bar {
    display: flex; gap: 12px; align-items: center; margin-bottom: 16px;
    padding: 10px 14px; background: #16213e; border: 1px solid #0f3460;
    border-radius: 8px; font-size: 11px;
  }
  .status-count { color: #7289da; font-weight: 500; }
  .status-spacer { flex: 1; }
  .auto-refresh-label { color: #555; }
</style>
</head>
<body>
<div class="container">
  <div class="header-row">
    <div>
      <h1>Audio Debug</h1>
      <div class="subtitle">Browse, play, and re-transcribe recorded utterances</div>
    </div>
    <a href="/" class="nav-link">Back to Control Panel</a>
  </div>

  <div class="status-bar">
    <span class="status-count" id="rec-count">0 recordings</span>
    <span class="status-spacer"></span>
    <label class="auto-refresh-label">
      <input type="checkbox" id="chk-auto" checked /> Auto-refresh (10s)
    </label>
    <button class="btn btn-refresh" id="btn-refresh" onclick="loadRecordings()">Refresh</button>
  </div>

  <div id="recordings-list">
    <div class="empty-state">Loading recordings...</div>
  </div>
</div>

<script>
const API = '';
let autoRefreshTimer = null;

async function loadRecordings() {
  try {
    const r = await fetch(API + '/debug/recordings');
    const d = await r.json();
    const recs = d.recordings || [];
    document.getElementById('rec-count').textContent = recs.length + ' recording' + (recs.length !== 1 ? 's' : '');

    const container = document.getElementById('recordings-list');
    if (recs.length === 0) {
      container.innerHTML = '<div class="empty-state">No recordings yet. Audio will be recorded when the bot receives speech and produces a transcript.</div>';
      return;
    }

    container.innerHTML = recs.map((rec, idx) => {
      const ts = rec.timestamp || rec.base || '?';
      const transcript = rec.transcript || '(no transcript)';
      const dur = rec.actual_duration_32k || rec.actual_duration_48k || rec.duration_s || '?';

      let audioHtml = '';
      if (rec.has_32k) {
        audioHtml += '<div class="audio-row">' +
          '<span class="audio-label">32kHz Mono</span>' +
          '<audio controls preload="none" src="' + API + '/debug/recordings/' + esc(rec.base) + '.32k.wav"></audio>' +
          '</div>';
      }
      if (rec.has_48k) {
        audioHtml += '<div class="audio-row">' +
          '<span class="audio-label">48kHz Stereo</span>' +
          '<audio controls preload="none" src="' + API + '/debug/recordings/' + esc(rec.base) + '.48k.wav"></audio>' +
          '</div>';
      }

      // Determine which file to retranscribe (prefer 32k)
      const retranscribeFile = rec.has_32k ? rec.base + '.32k.wav' : (rec.has_48k ? rec.base + '.48k.wav' : '');

      let retranscribeHtml = '';
      if (retranscribeFile) {
        retranscribeHtml = '<div class="retranscribe-row">' +
          '<button class="btn btn-primary" onclick="retranscribe(\'' + esc(retranscribeFile) + '\', \'medium.en\', ' + idx + ')">Re-transcribe (medium.en)</button>' +
          '<button class="btn btn-primary" onclick="retranscribe(\'' + esc(retranscribeFile) + '\', \'large-v3\', ' + idx + ')">Re-transcribe (large-v3)</button>' +
          '</div>' +
          '<div id="retranscript-' + idx + '"></div>';
      }

      return '<div class="recording">' +
        '<div class="rec-header">' +
          '<span class="rec-time">' + esc(formatTimestamp(ts)) + '</span>' +
          '<span class="rec-transcript">' + esc(transcript) + '</span>' +
          '<span class="rec-duration">' + dur + 's</span>' +
        '</div>' +
        audioHtml +
        retranscribeHtml +
        '</div>';
    }).join('');

  } catch (e) {
    document.getElementById('recordings-list').innerHTML =
      '<div class="empty-state">Error loading recordings: ' + esc(e.message) + '</div>';
  }
}

async function retranscribe(filename, model, idx) {
  const resultDiv = document.getElementById('retranscript-' + idx);
  if (!resultDiv) return;

  // Add a loading indicator
  const loadingId = 'loading-' + model.replace(/[^a-z0-9]/g, '') + '-' + idx;
  resultDiv.innerHTML += '<div id="' + loadingId + '" class="retranscript-result" style="opacity:0.5;">' +
    '<span class="model-tag">' + esc(model) + '</span> transcribing...' +
    '</div>';

  try {
    const r = await fetch(API + '/debug/retranscribe/' + encodeURIComponent(filename), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: model }),
    });
    const d = await r.json();
    const loadEl = document.getElementById(loadingId);
    if (!loadEl) return;

    if (d.ok) {
      loadEl.style.opacity = '1';
      loadEl.innerHTML = '<span class="model-tag">' + esc(model) + '</span>' +
        '<div class="result-text">' + esc(d.transcript) + '</div>' +
        '<div style="font-size:9px;color:#555;margin-top:2px;">' +
          (d.duration_s || '?') + 's audio, ' + (d.pcm_bytes || '?') + ' bytes PCM' +
        '</div>';
    } else {
      loadEl.style.opacity = '1';
      loadEl.className = 'retranscript-result error';
      loadEl.innerHTML = '<span class="model-tag">' + esc(model) + ' - ERROR</span>' +
        '<div class="result-text">' + esc(d.error || 'Unknown error') + '</div>';
    }
  } catch (e) {
    const loadEl = document.getElementById(loadingId);
    if (loadEl) {
      loadEl.style.opacity = '1';
      loadEl.className = 'retranscript-result error';
      loadEl.innerHTML = '<span class="model-tag">' + esc(model) + ' - ERROR</span>' +
        '<div class="result-text">' + esc(e.message) + '</div>';
    }
  }
}

function formatTimestamp(ts) {
  // Convert "20260216_233500" to "2026-02-16 23:35:00"
  if (ts.length >= 15 && ts[8] === '_') {
    return ts.substring(0, 4) + '-' + ts.substring(4, 6) + '-' + ts.substring(6, 8) +
      ' ' + ts.substring(9, 11) + ':' + ts.substring(11, 13) + ':' + ts.substring(13, 15);
  }
  return ts;
}

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function setupAutoRefresh() {
  const chk = document.getElementById('chk-auto');
  if (autoRefreshTimer) clearInterval(autoRefreshTimer);
  if (chk.checked) {
    autoRefreshTimer = setInterval(loadRecordings, 10000);
  }
  chk.onchange = setupAutoRefresh;
}

// Init
loadRecordings();
setupAutoRefresh();
</script>
</body>
</html>
"""
