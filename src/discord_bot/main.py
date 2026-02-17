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
from fastapi.responses import HTMLResponse
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
    return VoiceSettingResponse(voice=new_voice)


@app.get("/settings", response_model=BotSettingsResponse)
async def get_settings():
    """Get current bot runtime settings."""
    resp = BotSettingsResponse(
        tts_url=settings.tts_qwen_url,
        tts_language=settings.tts_qwen_language,
        llm_url=settings.llm_url,
        llm_temperature=settings.llm_temperature,
        llm_max_tokens=settings.llm_max_tokens,
        system_prompt=settings.system_prompt,
        debounce_seconds=settings.debounce_seconds,
    )
    # Get live runtime values from providers
    if _voice_bot:
        if hasattr(_voice_bot._tts, "_voice"):
            resp.tts_voice = _voice_bot._tts._voice
        if hasattr(_voice_bot._llm, "_model"):
            resp.llm_model = _voice_bot._llm._model
    else:
        resp.tts_voice = settings.tts_qwen_voice
        resp.llm_model = settings.llm_model
    return resp


@app.put("/settings", response_model=BotSettingsResponse)
async def update_settings(req: BotSettingsRequest):
    """Update bot settings at runtime (no restart needed)."""
    if not _voice_bot:
        raise HTTPException(503, "Bot not initialized")

    if req.tts_voice is not None and hasattr(_voice_bot._tts, "_voice"):
        _voice_bot._tts._voice = req.tts_voice
        settings.tts_qwen_voice = req.tts_voice
        logger.info("TTS voice changed to: %s", req.tts_voice)

    if req.llm_model is not None and hasattr(_voice_bot._llm, "_model"):
        _voice_bot._llm._model = req.llm_model
        settings.llm_model = req.llm_model
        logger.info("LLM model changed to: %s", req.llm_model)

    if req.llm_temperature is not None:
        settings.llm_temperature = req.llm_temperature

    if req.llm_max_tokens is not None:
        settings.llm_max_tokens = req.llm_max_tokens

    if req.system_prompt is not None:
        settings.system_prompt = req.system_prompt

    if req.debounce_seconds is not None:
        settings.debounce_seconds = req.debounce_seconds

    return await get_settings()


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
  document.getElementById('btn-bench').onclick = runBench;
  document.getElementById('btn-bench-all').onclick = () => {
    document.getElementById('bench-sel').value = '__all__';
    runBench();
  };

  // Slider live labels
  document.getElementById('rng-temp').oninput = (e) => {
    document.getElementById('lbl-temp').textContent = parseFloat(e.target.value).toFixed(1);
  };
  document.getElementById('rng-tokens').oninput = (e) => {
    document.getElementById('lbl-tokens').textContent = e.target.value;
  };
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
    } else {
      dot.className = 'status-dot';
      label.textContent = 'Not in voice channel';
      detail.textContent = d.stt_provider ? ('stt: ' + d.stt_provider + ' | llm: ' + d.llm_provider + ' | tts: ' + d.tts_provider) : '';
      uptime.textContent = '';
    }
  } catch (e) {
    document.getElementById('status-label').textContent = 'Service error';
  }
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
    const d = await r.json();
    toast('voice-toast', 'Voice set to: ' + d.tts_voice, 'ok');
  } catch (e) { toast('voice-toast', 'Error: ' + e.message, 'err'); }
}

async function applyModel() {
  const model = document.getElementById('sel-model').value;
  const temp = parseFloat(document.getElementById('rng-temp').value);
  const tokens = parseInt(document.getElementById('rng-tokens').value);
  try {
    const r = await fetch(API + '/settings', {
      method: 'PUT', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ llm_model: model, llm_temperature: temp, llm_max_tokens: tokens }),
    });
    const d = await r.json();
    toast('model-toast', 'Model: ' + d.llm_model + ' | temp: ' + d.llm_temperature + ' | tokens: ' + d.llm_max_tokens, 'ok');
  } catch (e) { toast('model-toast', 'Error: ' + e.message, 'err'); }
}

async function applyPrompt() {
  const prompt = document.getElementById('txt-prompt').value;
  try {
    const r = await fetch(API + '/settings', {
      method: 'PUT', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ system_prompt: prompt }),
    });
    if (r.ok) toast('prompt-toast', 'Saved', 'ok');
    else toast('prompt-toast', 'Error', 'err');
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
