#!/usr/bin/env python3
"""Voice Chat FastAPI service — local AI voice assistant with controllable devices.

Exposes REST API on port 8797 for starting/stopping voice chat,
selecting audio devices, and viewing conversation history.
Designed to be controlled from the Tauri desktop app.

Usage:
    python -m voice_chat.service
    # or
    python src/voice_chat/service.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import sounddevice as sd
import uvicorn
import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# Add src/ to path so we can import zoom_bot providers
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from zoom_bot.audio_pipeline import AudioPipeline
from zoom_bot.llm_providers import OpenAICompatibleLLM
from zoom_bot.stt_providers import FasterWhisperBatchSTT
from zoom_bot.tts_providers import QwenTTS

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

# Ensure all relevant loggers write to stderr even under uvicorn
for _name in ("voice-chat", "zoom_bot.audio_pipeline", "zoom_bot.stt_providers",
              "zoom_bot.llm_providers", "zoom_bot.tts_providers", "faster_whisper", "httpx"):
    _lg = logging.getLogger(_name)
    _lg.setLevel(logging.INFO)
    if not _lg.handlers:
        _h = logging.StreamHandler(sys.stderr)
        _h.setFormatter(logging.Formatter(LOG_FORMAT))
        _lg.addHandler(_h)
        _lg.propagate = False

logger = logging.getLogger("voice-chat")

# ---- Config from env ----
PORT = int(os.environ.get("VOICE_CHAT_PORT", "8797"))
STT_MODEL = os.environ.get("STT_MODEL", "base.en")
LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:11434")
LLM_MODEL = os.environ.get("LLM_MODEL", "llama3.1:8b")
TTS_URL = os.environ.get("TTS_URL", "http://toddllm:8790")
TTS_VOICE = os.environ.get("TTS_VOICE", "Vivian")
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", (
    "You are a friendly AI voice assistant. "
    "Keep every response to ONE short sentence (under 15 words). "
    "Be conversational and natural."
))
SPEECH_THRESHOLD = float(os.environ.get("SPEECH_THRESHOLD", "0.008"))

# Audio constants
CAPTURE_RATE = 48000
PIPELINE_RATE = 32000
PLAYBACK_RATE = 48000


# ---- Resampling helpers ----

def resample_48k_to_32k_mono(pcm_48k_mono_f32: np.ndarray) -> bytes:
    ratio = PIPELINE_RATE / CAPTURE_RATE
    out_len = int(len(pcm_48k_mono_f32) * ratio)
    indices = np.arange(out_len) / ratio
    idx = indices.astype(int)
    frac = indices - idx
    safe_idx = np.minimum(idx + 1, len(pcm_48k_mono_f32) - 1)
    resampled = pcm_48k_mono_f32[idx] * (1 - frac) + pcm_48k_mono_f32[safe_idx] * frac
    pcm_s16 = np.clip(resampled * 32767, -32768, 32767).astype(np.int16)
    return pcm_s16.tobytes()


def resample_32k_to_48k(pcm_32k: bytes) -> np.ndarray:
    n_samples = len(pcm_32k) // 2
    if n_samples == 0:
        return np.array([], dtype=np.float32)
    samples = np.frombuffer(pcm_32k, dtype=np.int16).astype(np.float32) / 32768.0
    ratio = PLAYBACK_RATE / PIPELINE_RATE
    out_len = int(n_samples * ratio)
    indices = np.arange(out_len) / ratio
    idx = indices.astype(int)
    frac = indices - idx
    safe_idx = np.minimum(idx + 1, n_samples - 1)
    resampled = samples[idx] * (1 - frac) + samples[safe_idx] * frac
    return resampled.astype(np.float32)


def find_device_index(name: str, kind: str) -> int | str:
    for i, d in enumerate(sd.query_devices()):
        if kind == "input" and d["max_input_channels"] == 0:
            continue
        if kind == "output" and d["max_output_channels"] == 0:
            continue
        if name.lower() in d["name"].lower():
            return i
    return name


# ---- Audio Player ----

class AudioPlayer:
    def __init__(self, device: str | int, sample_rate: int = PLAYBACK_RATE):
        self._device = device
        self._sample_rate = sample_rate
        self.is_playing = threading.Event()

    def set_device(self, device: str | int) -> None:
        self._device = device

    def push_audio(self, pcm_32k: bytes) -> None:
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


# ---- Log ring buffer ----

import collections

LOG_RING: collections.deque[dict] = collections.deque(maxlen=200)


class RingLogHandler(logging.Handler):
    """Captures log records into a ring buffer for the web UI."""
    def emit(self, record):
        try:
            LOG_RING.append({
                "ts": record.created,
                "level": record.levelname,
                "name": record.name,
                "msg": self.format(record),
            })
        except Exception:
            pass


_ring_handler = RingLogHandler()
_ring_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
for _name in ("voice-chat", "zoom_bot.audio_pipeline", "zoom_bot.stt_providers",
              "zoom_bot.llm_providers", "zoom_bot.tts_providers", "faster_whisper"):
    logging.getLogger(_name).addHandler(_ring_handler)


# ---- Voice Chat Session ----

@dataclass
class ConversationEntry:
    role: str       # "user" or "assistant"
    text: str
    timestamp: float


@dataclass
class VoiceChatSession:
    """Manages a running voice chat session."""
    pipeline: AudioPipeline | None = None
    player: AudioPlayer | None = None
    mic_stream: Any = None
    mic_device: str = "CaptureMic 2ch"
    speaker_device: str = "CaptureAudio 2ch"
    running: bool = False
    state: str = "idle"  # idle, listening, thinking, speaking
    conversation: list[ConversationEntry] = field(default_factory=list)
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _loop: asyncio.AbstractEventLoop | None = None
    # Live audio level tracking
    mic_rms: float = 0.0
    mic_peak: float = 0.0
    mic_chunks: int = 0


session = VoiceChatSession()


# ---- FastAPI ----

app = FastAPI(title="Voice Chat", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Models ---

class DeviceInfo(BaseModel):
    index: int
    name: str
    kind: str  # "input", "output", "input/output"
    channels_in: int
    channels_out: int
    sample_rate: float


class StatusResponse(BaseModel):
    running: bool
    state: str
    mic_device: str
    speaker_device: str
    stt_model: str
    llm_model: str
    tts_voice: str
    conversation_length: int


class StartRequest(BaseModel):
    mic_device: str | None = None
    speaker_device: str | None = None


class ConfigRequest(BaseModel):
    mic_device: str | None = None
    speaker_device: str | None = None
    stt_model: str | None = None
    llm_model: str | None = None
    tts_voice: str | None = None
    system_prompt: str | None = None
    speech_threshold: float | None = None


# --- Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def index():
    return WEB_UI_HTML


@app.get("/health")
async def health():
    return {"status": "ok", "service": "voice-chat", "port": PORT}


@app.get("/api/levels")
async def get_levels():
    """Live audio levels from the mic input."""
    import math
    rms_db = 20 * math.log10(max(session.mic_rms, 1e-10))
    peak_db = 20 * math.log10(max(session.mic_peak, 1e-10))
    speech_thresh = FasterWhisperBatchSTT.SPEECH_THRESHOLD
    return {
        "rms": round(session.mic_rms, 6),
        "peak": round(session.mic_peak, 6),
        "rms_db": round(rms_db, 1),
        "peak_db": round(peak_db, 1),
        "speech_threshold": speech_thresh,
        "above_threshold": session.mic_rms >= speech_thresh,
        "chunks_received": session.mic_chunks,
    }


@app.get("/api/logs")
async def get_logs(n: int = 50):
    """Return recent log entries."""
    entries = list(LOG_RING)[-n:]
    return {"logs": entries}


@app.get("/api/models")
async def list_models():
    """List available Ollama models."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{LLM_URL}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            return {"models": [m["name"] for m in data.get("models", [])]}
    except Exception:
        return {"models": [LLM_MODEL]}


@app.get("/api/devices")
async def list_devices():
    """List all available audio input and output devices."""
    devices = sd.query_devices()
    result = {"inputs": [], "outputs": []}
    for i, d in enumerate(devices):
        info = {
            "index": i,
            "name": d["name"],
            "channels_in": d["max_input_channels"],
            "channels_out": d["max_output_channels"],
            "sample_rate": d["default_samplerate"],
        }
        if d["max_input_channels"] > 0:
            result["inputs"].append(info)
        if d["max_output_channels"] > 0:
            result["outputs"].append(info)
    return result


@app.get("/api/status")
async def get_status():
    return StatusResponse(
        running=session.running,
        state=session.state,
        mic_device=session.mic_device,
        speaker_device=session.speaker_device,
        stt_model=STT_MODEL,
        llm_model=LLM_MODEL,
        tts_voice=TTS_VOICE,
        conversation_length=len(session.conversation),
    )


@app.get("/api/conversation")
async def get_conversation():
    """Return conversation history."""
    return [
        {"role": e.role, "text": e.text, "timestamp": e.timestamp}
        for e in session.conversation
    ]


@app.post("/api/start")
async def start_chat(req: StartRequest | None = None):
    """Start the voice chat pipeline."""
    if session.running:
        return {"ok": False, "error": "already running"}

    if req and req.mic_device:
        session.mic_device = req.mic_device
    if req and req.speaker_device:
        session.speaker_device = req.speaker_device

    session.conversation.clear()
    session._loop = asyncio.get_event_loop()

    try:
        await _start_pipeline()
        return {"ok": True, "mic": session.mic_device, "speaker": session.speaker_device}
    except Exception as e:
        logger.exception("Failed to start voice chat")
        return {"ok": False, "error": str(e)}


@app.post("/api/stop")
async def stop_chat():
    """Stop the voice chat pipeline."""
    if not session.running:
        return {"ok": False, "error": "not running"}

    await _stop_pipeline()
    return {"ok": True}


@app.post("/api/config")
async def update_config(req: ConfigRequest):
    """Update configuration (some changes take effect on next start)."""
    global STT_MODEL, LLM_MODEL, TTS_VOICE, SYSTEM_PROMPT, LLM_URL, TTS_URL, SPEECH_THRESHOLD

    changed = {}
    if req.mic_device is not None:
        session.mic_device = req.mic_device
        changed["mic_device"] = req.mic_device
    if req.speaker_device is not None:
        session.speaker_device = req.speaker_device
        changed["speaker_device"] = req.speaker_device
        # Update player device live if running
        if session.player:
            speaker_idx = find_device_index(req.speaker_device, "output")
            session.player.set_device(speaker_idx)
    if req.stt_model is not None:
        STT_MODEL = req.stt_model
        changed["stt_model"] = req.stt_model
    if req.llm_model is not None:
        LLM_MODEL = req.llm_model
        changed["llm_model"] = req.llm_model
    if req.tts_voice is not None:
        TTS_VOICE = req.tts_voice
        changed["tts_voice"] = req.tts_voice
    if req.system_prompt is not None:
        SYSTEM_PROMPT = req.system_prompt
        changed["system_prompt"] = req.system_prompt
    if req.speech_threshold is not None:
        SPEECH_THRESHOLD = req.speech_threshold
        changed["speech_threshold"] = req.speech_threshold

    return {"ok": True, "changed": changed, "note": "restart required for STT/LLM/threshold changes"}


# ---- TTS proxy endpoints ----

@app.get("/api/voices")
async def list_voices():
    """Proxy to TTS service profiles endpoint."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{TTS_URL}/v1/profiles")
            resp.raise_for_status()
            return resp.json()
    except Exception:
        # Fallback to basic voices list
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{TTS_URL}/v1/voices")
                resp.raise_for_status()
                data = resp.json()
                return {"profiles": [
                    {"name": v, "display_name": v, "voice_type": "builtin", "is_owner": False, "has_photo": False}
                    for v in data.get("voices", [])
                ]}
        except Exception:
            return {"profiles": [
                {"name": "Vivian", "display_name": "Vivian", "voice_type": "builtin", "is_owner": False, "has_photo": False},
            ]}


@app.get("/api/tts/profiles")
async def proxy_tts_profiles():
    """Proxy to TTS service profiles for Tauri app."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{TTS_URL}/v1/profiles")
        resp.raise_for_status()
        return resp.json()


@app.post("/api/tts/profiles/clone")
async def proxy_tts_clone(body: dict):
    """Proxy clone request to TTS service."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(f"{TTS_URL}/v1/profiles/clone", json=body)
        if resp.status_code != 200:
            from fastapi import Response
            return Response(content=resp.content, status_code=resp.status_code,
                            media_type="application/json")
        return resp.json()


@app.delete("/api/tts/profiles/{name}")
async def proxy_tts_delete(name: str):
    """Proxy delete request to TTS service."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.delete(f"{TTS_URL}/v1/profiles/{name}")
        if resp.status_code != 200:
            from fastapi import Response
            return Response(content=resp.content, status_code=resp.status_code,
                            media_type="application/json")
        return resp.json()


@app.post("/api/tts/profiles/{name}/photo")
async def proxy_tts_photo_upload(name: str, body: dict):
    """Proxy photo upload to TTS service."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(f"{TTS_URL}/v1/profiles/{name}/photo", json=body)
        if resp.status_code != 200:
            from fastapi import Response
            return Response(content=resp.content, status_code=resp.status_code,
                            media_type="application/json")
        return resp.json()


@app.get("/api/tts/profiles/{name}/photo")
async def proxy_tts_photo_get(name: str):
    """Proxy photo retrieval from TTS service."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{TTS_URL}/v1/profiles/{name}/photo")
        if resp.status_code != 200:
            from fastapi import Response
            return Response(content=resp.content, status_code=resp.status_code,
                            media_type="application/json")
        from fastapi.responses import Response
        return Response(
            content=resp.content,
            media_type=resp.headers.get("content-type", "image/jpeg"),
        )


@app.post("/api/tts/profiles/{name}/test")
async def proxy_tts_test(name: str):
    """Proxy test voice request to TTS service."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{TTS_URL}/v1/profiles/{name}/test")
        if resp.status_code != 200:
            from fastapi import Response
            return Response(content=resp.content, status_code=resp.status_code,
                            media_type="application/json")
        return resp.json()


# ---- Pipeline lifecycle ----

async def _start_pipeline():
    """Initialize and start the STT -> LLM -> TTS pipeline + mic capture."""
    stt = FasterWhisperBatchSTT(model=STT_MODEL, silence_ms=800,
                                speech_threshold=SPEECH_THRESHOLD,
                                silence_threshold=SPEECH_THRESHOLD / 2)
    llm = OpenAICompatibleLLM(
        base_url=LLM_URL,
        model=LLM_MODEL,
        api_key="",
        timeout=30.0,
    )
    tts = QwenTTS(url=TTS_URL, voice=TTS_VOICE)

    speaker_idx = find_device_index(session.speaker_device, "output")
    player = AudioPlayer(device=speaker_idx)

    # Wrap the TTS callback to track conversation + state
    original_push = player.push_audio

    def tracked_push(pcm_32k: bytes):
        session.state = "speaking"
        original_push(pcm_32k)
        session.state = "listening"

    pipeline = AudioPipeline(
        stt=stt,
        llm=llm,
        tts=tts,
        system_prompt=SYSTEM_PROMPT,
        on_tts_audio=tracked_push,
        debounce_seconds=0.8,
        llm_max_tokens=80,
        llm_temperature=0.5,
    )

    # Hook into pipeline to track conversation entries
    _hook_conversation_tracking(pipeline, stt)

    logger.info("Loading Whisper model...")
    await pipeline.start()
    logger.info("Pipeline started. Opening mic...")

    mic_idx = find_device_index(session.mic_device, "input")
    logger.info("Mic device resolved: '%s' -> index %s", session.mic_device, mic_idx)
    logger.info("Speaker device resolved: '%s' -> index %s", session.speaker_device, speaker_idx)
    stop_event = threading.Event()

    # Audio level logging: log every ~2 seconds
    _level_log_counter = [0]

    def audio_callback(indata, frames, time_info, status):
        if status:
            logger.warning("Mic status: %s", status)
        mono = indata[:, 0] if indata.shape[1] > 1 else indata.flatten()
        rms = float(np.sqrt(np.mean(mono ** 2)))
        peak = float(np.max(np.abs(mono)))
        session.mic_rms = rms
        session.mic_peak = peak
        session.mic_chunks += 1

        # Log levels periodically (every ~2s = 20 callbacks at 100ms each)
        _level_log_counter[0] += 1
        if _level_log_counter[0] % 20 == 0:
            import math
            rms_db = 20 * math.log10(max(rms, 1e-10))
            thresh = FasterWhisperBatchSTT.SPEECH_THRESHOLD
            above = "SPEECH" if rms >= thresh else "quiet"
            logger.info("Mic level: RMS=%.4f (%.0f dB) peak=%.4f [%s] thresh=%.3f",
                        rms, rms_db, peak, above, thresh)

        if player.is_playing.is_set():
            return
        pcm_32k = resample_48k_to_32k_mono(mono)
        pipeline.feed_audio(pcm_32k)

    # Determine channels — CaptureMic is 2ch, MacBook mic is 1ch
    dev_info = sd.query_devices(mic_idx)
    mic_channels = min(dev_info["max_input_channels"], 2) if isinstance(dev_info, dict) else 1
    logger.info("Opening mic: %s (%d channels, %dHz)", session.mic_device, mic_channels, CAPTURE_RATE)

    stream = sd.InputStream(
        device=mic_idx,
        samplerate=CAPTURE_RATE,
        channels=mic_channels,
        dtype="float32",
        blocksize=4800,
        callback=audio_callback,
    )
    stream.start()
    logger.info("Mic stream started OK")

    session.pipeline = pipeline
    session.player = player
    session.mic_stream = stream
    session._stop_event = stop_event
    session.running = True
    session.state = "listening"
    logger.info("Voice chat READY: mic=%s, speaker=%s, stt=%s, llm=%s",
                session.mic_device, session.speaker_device, STT_MODEL, LLM_MODEL)


def _hook_conversation_tracking(pipeline: AudioPipeline, stt: FasterWhisperBatchSTT):
    """Hook pipeline to record conversation entries for the UI."""
    # Hook STT transcript emission to track user messages
    orig_emit = stt._emit_transcript

    def tracked_emit(text: str):
        session.conversation.append(ConversationEntry(
            role="user", text=text, timestamp=time.time()
        ))
        session.state = "thinking"
        orig_emit(text)

    stt._emit_transcript = tracked_emit

    # Hook LLM chat to track assistant responses
    orig_llm_chat = pipeline._llm.chat

    async def tracked_llm_chat(messages, **kwargs):
        result = await orig_llm_chat(messages, **kwargs)
        if result:
            session.conversation.append(ConversationEntry(
                role="assistant", text=result, timestamp=time.time()
            ))
        return result

    pipeline._llm.chat = tracked_llm_chat


async def _stop_pipeline():
    """Stop the voice chat pipeline and release resources."""
    session.running = False
    session.state = "idle"
    session._stop_event.set()

    if session.mic_stream:
        try:
            session.mic_stream.stop()
            session.mic_stream.close()
        except Exception:
            pass
        session.mic_stream = None

    if session.pipeline:
        try:
            await session.pipeline.stop()
        except Exception:
            pass
        session.pipeline = None

    session.player = None
    logger.info("Voice chat stopped.")


# ---- Web UI ----

WEB_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Voice Chat</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    background: #1a1a2e; color: #e0e0e0;
    min-height: 100vh; padding: 20px;
  }
  .container { max-width: 1100px; margin: 0 auto; }
  h1 { font-size: 20px; font-weight: 600; color: #e94560; margin-bottom: 4px; }
  .subtitle { font-size: 11px; color: #555; margin-bottom: 20px; }

  .grid { display: grid; grid-template-columns: 320px 1fr; gap: 16px; }
  @media (max-width: 700px) { .grid { grid-template-columns: 1fr; } }

  .panel {
    background: #16213e; border: 1px solid #0f3460;
    border-radius: 8px; padding: 16px;
  }
  .panel-title {
    font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 1px; color: #0f3460; margin-bottom: 12px;
  }

  /* Status */
  .status-row { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
  .status-dot {
    width: 12px; height: 12px; border-radius: 50%;
    background: #555; transition: all 0.3s;
  }
  .status-dot.idle { background: #555; }
  .status-dot.listening { background: #00e676; box-shadow: 0 0 8px #00e676; animation: pulse 2s ease-in-out infinite; }
  .status-dot.thinking { background: #ffc107; box-shadow: 0 0 8px #ffc107; animation: pulse 0.8s ease-in-out infinite; }
  .status-dot.speaking { background: #2196f3; box-shadow: 0 0 10px #2196f3; animation: pulse 0.5s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.4; } }
  .status-label { font-size: 14px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
  .status-label.idle { color: #555; }
  .status-label.listening { color: #00e676; }
  .status-label.thinking { color: #ffc107; }
  .status-label.speaking { color: #2196f3; }

  /* Mic level meter */
  .level-section { margin-bottom: 12px; }
  .level-row { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
  .level-label { font-size: 10px; color: #666; width: 32px; }
  .level-meter { flex: 1; height: 8px; background: #0f3460; border-radius: 4px; overflow: hidden; position: relative; }
  .level-bar { height: 100%; border-radius: 4px; transition: width 0.1s; background: #00e676; }
  .level-bar.hot { background: #ffc107; }
  .level-bar.speech { background: #00e676; box-shadow: 0 0 4px #00e676; }
  .level-bar.silent { background: #555; }
  .level-thresh { position: absolute; top: 0; bottom: 0; width: 2px; background: #e94560; }
  .level-db { font-size: 10px; color: #666; width: 48px; text-align: right; font-variant-numeric: tabular-nums; }
  .level-info { font-size: 9px; color: #444; }
  .level-info .above { color: #00e676; font-weight: 600; }
  .level-info .below { color: #555; }

  /* Form */
  .form-row { margin-bottom: 10px; }
  .form-label { display: block; font-size: 10px; color: #888; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
  select, input[type=text], textarea {
    width: 100%; font-size: 12px; padding: 6px 8px;
    background: #1a1a2e; color: #e0e0e0;
    border: 1px solid #0f3460; border-radius: 4px; outline: none;
  }
  select:focus, input:focus, textarea:focus { border-color: #e94560; }
  textarea { resize: vertical; min-height: 52px; font-family: inherit; }

  .btn-row { display: flex; gap: 8px; margin-top: 14px; margin-bottom: 10px; }
  .btn {
    flex: 1; padding: 10px 16px; font-size: 13px; font-weight: 600;
    border: none; border-radius: 6px; cursor: pointer; transition: all 0.15s;
  }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-start { background: #00e676; color: #1a1a2e; }
  .btn-start:hover:not(:disabled) { background: #00c853; }
  .btn-stop { background: #ff5252; color: #fff; }
  .btn-stop:hover:not(:disabled) { background: #d32f2f; }
  .btn-apply {
    background: transparent; border: 1px solid #0f3460; color: #888;
    font-size: 11px; padding: 6px 12px; border-radius: 4px; cursor: pointer;
    margin-top: 4px;
  }
  .btn-apply:hover { border-color: #e94560; color: #e0e0e0; }

  /* Right column: stacked conversation + logs */
  .right-col { display: flex; flex-direction: column; gap: 12px; }

  /* Conversation */
  .conversation { flex: 1; overflow-y: auto; max-height: 280px; display: flex; flex-direction: column; gap: 6px; }
  .msg { padding: 8px 12px; border-radius: 6px; display: flex; gap: 8px; align-items: flex-start; }
  .msg-user { background: rgba(0,230,118,0.08); }
  .msg-ai { background: rgba(33,150,243,0.08); }
  .msg-label { font-size: 10px; font-weight: 700; text-transform: uppercase; flex-shrink: 0; width: 28px; padding-top: 2px; }
  .msg-user .msg-label { color: #00e676; }
  .msg-ai .msg-label { color: #2196f3; }
  .msg-text { font-size: 13px; line-height: 1.5; flex: 1; }
  .msg-time { font-size: 9px; color: #444; flex-shrink: 0; font-variant-numeric: tabular-nums; padding-top: 3px; }
  .empty-conv { color: #444; font-size: 12px; font-style: italic; text-align: center; padding: 30px 0; }

  /* Logs */
  .log-box {
    overflow-y: auto; max-height: 250px; min-height: 100px;
    font-family: "SF Mono", "Fira Code", monospace; font-size: 10px;
    line-height: 1.5; color: #8b949e; white-space: pre-wrap; word-break: break-all;
    background: #0d1117; border-radius: 4px; padding: 8px;
  }
  .log-line { margin-bottom: 1px; }
  .log-line.error { color: #ff5252; }
  .log-line.warning { color: #ffc107; }
  .log-line .log-name { color: #555; }

  /* Provider info */
  .provider-info { display: flex; gap: 8px; flex-wrap: wrap; padding: 8px 0; margin-top: 8px; }
  .provider-tag { font-size: 10px; color: #555; background: rgba(15,52,96,0.4); padding: 2px 8px; border-radius: 3px; }
  .provider-tag b { color: #888; font-weight: 500; }

  /* Tabs */
  .tabs { display: flex; gap: 0; margin-bottom: 0; }
  .tab {
    font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;
    padding: 8px 16px; cursor: pointer; color: #555;
    border-bottom: 2px solid transparent; transition: all 0.15s;
  }
  .tab:hover { color: #888; }
  .tab.active { color: #e94560; border-bottom-color: #e94560; }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  .toast {
    position: fixed; bottom: 20px; right: 20px;
    background: #16213e; border: 1px solid #0f3460;
    border-radius: 6px; padding: 10px 16px;
    font-size: 12px; color: #e0e0e0;
    opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 1000;
  }
  .toast.show { opacity: 1; }
  .toast.error { border-color: #ff5252; color: #ff5252; }
</style>
</head>
<body>
<div class="container">
  <h1>Voice Chat</h1>
  <div class="subtitle">Local AI voice assistant &mdash; STT + LLM + TTS</div>

  <div class="grid">
    <!-- Left: Controls -->
    <div>
      <div class="panel">
        <div class="panel-title">Status</div>
        <div class="status-row">
          <div class="status-dot idle" id="dot"></div>
          <span class="status-label idle" id="state-label">offline</span>
        </div>

        <div class="level-section" id="level-section" style="display:none;">
          <div class="level-row">
            <span class="level-label">RMS</span>
            <div class="level-meter">
              <div class="level-bar silent" id="level-bar" style="width:0%"></div>
              <div class="level-thresh" id="level-thresh" style="left:50%"></div>
            </div>
            <span class="level-db" id="level-db">&mdash;</span>
          </div>
          <div class="level-info" id="level-info"></div>
        </div>

        <div class="btn-row">
          <button class="btn btn-start" id="btn-start" disabled>Start</button>
          <button class="btn btn-stop" id="btn-stop" disabled>Stop</button>
        </div>
      </div>

      <div class="panel" style="margin-top: 12px;">
        <div class="panel-title">Audio Devices</div>
        <div class="form-row">
          <label class="form-label">Microphone</label>
          <select id="sel-mic"><option>loading...</option></select>
        </div>
        <div class="form-row">
          <label class="form-label">Speaker / Headphones</label>
          <select id="sel-speaker"><option>loading...</option></select>
        </div>
      </div>

      <div class="panel" style="margin-top: 12px;">
        <div class="panel-title">Model Configuration</div>
        <div class="form-row">
          <label class="form-label">LLM Model</label>
          <select id="sel-llm"><option>loading...</option></select>
        </div>
        <div class="form-row">
          <label class="form-label">STT Model (Whisper)</label>
          <select id="sel-stt">
            <option value="tiny.en">tiny.en (fastest)</option>
            <option value="base.en" selected>base.en (default)</option>
            <option value="small.en">small.en (better accuracy)</option>
            <option value="medium.en">medium.en (best accuracy)</option>
          </select>
        </div>
        <div class="form-row">
          <label class="form-label">TTS Voice</label>
          <select id="sel-tts"><option>loading...</option></select>
        </div>
        <div class="form-row">
          <label class="form-label">System Prompt</label>
          <textarea id="txt-prompt" rows="3"></textarea>
        </div>
        <button class="btn-apply" id="btn-apply">Apply Config</button>
      </div>

      <div class="provider-info" id="provider-info"></div>
    </div>

    <!-- Right: Conversation + Logs -->
    <div class="right-col">
      <div class="panel" style="flex: 1; display: flex; flex-direction: column;">
        <div class="tabs">
          <div class="tab active" data-tab="conv">Conversation</div>
          <div class="tab" data-tab="logs">Logs</div>
        </div>
        <div class="tab-content active" id="tab-conv" style="flex:1; display:flex; flex-direction:column;">
          <div class="conversation" id="conversation">
            <div class="empty-conv">Start the voice chat and speak into your microphone</div>
          </div>
        </div>
        <div class="tab-content" id="tab-logs" style="flex:1; display:flex; flex-direction:column;">
          <div class="log-box" id="log-box">Waiting for logs...</div>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const API = '';
let running = false;
let lastConvLen = 0;
let lastLogCount = 0;
let activeTab = 'conv';

async function init() {
  await Promise.all([loadDevices(), loadModels(), loadVoices(), loadStatus()]);
  setInterval(pollStatus, 1500);
  setInterval(pollConversation, 800);
  setInterval(pollLevels, 200);
  setInterval(pollLogs, 1000);

  // Tab switching
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      activeTab = tab.dataset.tab;
      document.getElementById('tab-' + activeTab).classList.add('active');
      if (activeTab === 'logs') pollLogs();
    });
  });
}

// --- Devices ---
async function loadDevices() {
  try {
    const r = await fetch(API + '/api/devices');
    const d = await r.json();
    fillSelect('sel-mic', d.inputs.map(x => x.name));
    fillSelect('sel-speaker', d.outputs.map(x => x.name));
  } catch {
    fillSelect('sel-mic', ['service unavailable']);
    fillSelect('sel-speaker', ['service unavailable']);
  }
}

// --- Models ---
async function loadModels() {
  try {
    const r = await fetch(API + '/api/models');
    const d = await r.json();
    const chatModels = d.models.filter(m =>
      !m.includes('embed') && !m.includes('nomic') && !m.includes('mxbai')
    );
    fillSelect('sel-llm', chatModels);
  } catch {
    fillSelect('sel-llm', ['llama3.1:8b']);
  }
}

// --- Voices ---
async function loadVoices() {
  try {
    const r = await fetch(API + '/api/voices');
    const d = await r.json();
    const sel = document.getElementById('sel-tts');
    const cloned = d.profiles.filter(p => p.voice_type === 'cloned');
    const builtin = d.profiles.filter(p => p.voice_type === 'builtin');
    let html = '';
    if (cloned.length > 0) {
      html += '<optgroup label="Cloned Voices">';
      cloned.forEach(p => { html += `<option value="${esc(p.name)}">${esc(p.display_name)}${p.is_owner ? ' (owner)' : ''}</option>`; });
      html += '</optgroup>';
    }
    html += '<optgroup label="Builtin Voices">';
    builtin.forEach(p => { html += `<option value="${esc(p.name)}">${esc(p.display_name)}</option>`; });
    html += '</optgroup>';
    sel.innerHTML = html;
  } catch {
    fillSelect('sel-tts', ['Vivian', 'Serena', 'Chelsie', 'Ethan']);
  }
}

// --- Status ---
async function loadStatus() {
  try {
    const r = await fetch(API + '/api/status');
    const s = await r.json();
    updateUI(s);
    selectOption('sel-mic', s.mic_device);
    selectOption('sel-speaker', s.speaker_device);
    selectOption('sel-llm', s.llm_model);
    selectOption('sel-stt', s.stt_model);
    selectOption('sel-tts', s.tts_voice);
    document.getElementById('btn-start').disabled = false;
  } catch {
    updateOffline();
  }
}

async function pollStatus() {
  try {
    const r = await fetch(API + '/api/status');
    const s = await r.json();
    updateUI(s);
  } catch { updateOffline(); }
}

function updateUI(s) {
  const dot = document.getElementById('dot');
  const label = document.getElementById('state-label');
  const st = s.running ? s.state : 'idle';
  dot.className = 'status-dot ' + st;
  label.className = 'status-label ' + st;
  label.textContent = s.running ? s.state : 'idle';
  running = s.running;
  document.getElementById('btn-start').disabled = s.running;
  document.getElementById('btn-stop').disabled = !s.running;
  document.getElementById('level-section').style.display = s.running ? '' : 'none';

  const info = document.getElementById('provider-info');
  info.innerHTML = `
    <span class="provider-tag"><b>STT:</b> ${s.stt_model}</span>
    <span class="provider-tag"><b>LLM:</b> ${s.llm_model}</span>
    <span class="provider-tag"><b>TTS:</b> ${s.tts_voice}</span>
    <span class="provider-tag"><b>Mic:</b> ${s.mic_device}</span>
    <span class="provider-tag"><b>Speaker:</b> ${s.speaker_device}</span>
  `;
}

function updateOffline() {
  document.getElementById('dot').className = 'status-dot';
  document.getElementById('state-label').className = 'status-label idle';
  document.getElementById('state-label').textContent = 'offline';
  document.getElementById('btn-start').disabled = true;
  document.getElementById('btn-stop').disabled = true;
  document.getElementById('level-section').style.display = 'none';
}

// --- Levels ---
async function pollLevels() {
  if (!running) return;
  try {
    const r = await fetch(API + '/api/levels');
    const lv = await r.json();
    const pct = Math.max(0, Math.min(100, ((lv.rms_db + 60) / 60) * 100));
    const bar = document.getElementById('level-bar');
    bar.style.width = pct + '%';
    bar.className = 'level-bar ' + (lv.above_threshold ? 'speech' : (pct > 5 ? 'hot' : 'silent'));

    // Position threshold marker
    const threshDb = 20 * Math.log10(Math.max(lv.speech_threshold, 1e-10));
    const threshPct = Math.max(0, Math.min(100, ((threshDb + 60) / 60) * 100));
    document.getElementById('level-thresh').style.left = threshPct + '%';

    document.getElementById('level-db').textContent = lv.rms_db > -100 ? lv.rms_db + ' dB' : '\\u2014';
    const infoEl = document.getElementById('level-info');
    if (lv.above_threshold) {
      infoEl.innerHTML = '<span class="above">SPEECH DETECTED</span> \\u2014 chunks: ' + lv.chunks_received;
    } else {
      infoEl.innerHTML = '<span class="below">below threshold</span> \\u2014 chunks: ' + lv.chunks_received;
    }
  } catch {}
}

// --- Conversation ---
async function pollConversation() {
  if (!running && lastConvLen === 0) return;
  try {
    const r = await fetch(API + '/api/conversation');
    const msgs = await r.json();
    if (msgs.length !== lastConvLen) {
      lastConvLen = msgs.length;
      renderConversation(msgs);
    }
  } catch {}
}

function renderConversation(msgs) {
  const el = document.getElementById('conversation');
  if (msgs.length === 0) {
    el.innerHTML = '<div class="empty-conv">Start the voice chat and speak into your microphone</div>';
    return;
  }
  el.innerHTML = msgs.map(m => {
    const cls = m.role === 'user' ? 'msg-user' : 'msg-ai';
    const label = m.role === 'user' ? 'You' : 'AI';
    const time = new Date(m.timestamp * 1000).toLocaleTimeString();
    return `<div class="msg ${cls}">
      <span class="msg-label">${label}</span>
      <span class="msg-text">${esc(m.text)}</span>
      <span class="msg-time">${time}</span>
    </div>`;
  }).join('');
  el.scrollTop = el.scrollHeight;
}

// --- Logs ---
async function pollLogs() {
  try {
    const r = await fetch(API + '/api/logs?n=80');
    const d = await r.json();
    if (d.logs.length === lastLogCount) return;
    lastLogCount = d.logs.length;
    const box = document.getElementById('log-box');
    box.innerHTML = d.logs.map(l => {
      let cls = 'log-line';
      if (l.level === 'ERROR') cls += ' error';
      if (l.level === 'WARNING') cls += ' warning';
      return `<div class="${cls}">${esc(l.msg)}</div>`;
    }).join('');
    box.scrollTop = box.scrollHeight;
  } catch {}
}

// --- Actions ---
document.getElementById('btn-start').addEventListener('click', async () => {
  const btn = document.getElementById('btn-start');
  btn.disabled = true; btn.textContent = 'Starting...';
  lastConvLen = 0;
  try {
    const body = {
      mic_device: document.getElementById('sel-mic').value,
      speaker_device: document.getElementById('sel-speaker').value,
    };
    const r = await fetch(API + '/api/start', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!d.ok) toast(d.error || 'Failed to start', true);
    else toast('Voice chat started');
  } catch(e) { toast('Error: ' + e.message, true); }
  btn.textContent = 'Start'; pollStatus();
});

document.getElementById('btn-stop').addEventListener('click', async () => {
  const btn = document.getElementById('btn-stop');
  btn.disabled = true; btn.textContent = 'Stopping...';
  try {
    await fetch(API + '/api/stop', { method:'POST', headers:{'Content-Type':'application/json'}, body:'{}' });
    toast('Voice chat stopped');
  } catch(e) { toast('Error: ' + e.message, true); }
  btn.textContent = 'Stop'; pollStatus();
});

document.getElementById('btn-apply').addEventListener('click', async () => {
  const config = {};
  const llm = document.getElementById('sel-llm').value;
  const stt = document.getElementById('sel-stt').value;
  const tts = document.getElementById('sel-tts').value;
  const mic = document.getElementById('sel-mic').value;
  const spk = document.getElementById('sel-speaker').value;
  const prompt = document.getElementById('txt-prompt').value.trim();
  if (llm) config.llm_model = llm;
  if (stt) config.stt_model = stt;
  if (tts) config.tts_voice = tts;
  if (mic) config.mic_device = mic;
  if (spk) config.speaker_device = spk;
  if (prompt) config.system_prompt = prompt;
  try {
    const r = await fetch(API + '/api/config', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(config),
    });
    const d = await r.json();
    if (d.ok) toast(running ? 'Applied (restart for full effect)' : 'Config saved');
  } catch(e) { toast('Error: ' + e.message, true); }
  pollStatus();
});

// --- Helpers ---
function fillSelect(id, opts) {
  document.getElementById(id).innerHTML = opts.map(o => `<option value="${esc(o)}">${esc(o)}</option>`).join('');
}
function selectOption(id, val) {
  const sel = document.getElementById(id);
  for (let i=0;i<sel.options.length;i++) { if (sel.options[i].value===val) { sel.selectedIndex=i; return; } }
  const lv = val.toLowerCase();
  for (let i=0;i<sel.options.length;i++) { if (sel.options[i].value.toLowerCase().includes(lv)) { sel.selectedIndex=i; return; } }
}
function esc(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
let toastTimer;
function toast(msg, err) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.className = 'toast show' + (err ? ' error' : '');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.className = 'toast'; }, 3000);
}

init();
</script>
</body>
</html>
"""


# ---- Entry point ----

def main():
    # Use log_config=None to prevent uvicorn from overriding our logging setup
    uvicorn.run(
        "voice_chat.service:app",
        host="127.0.0.1",
        port=PORT,
        log_level="info",
        log_config=None,
    )


if __name__ == "__main__":
    main()
