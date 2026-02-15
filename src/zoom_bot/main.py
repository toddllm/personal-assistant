"""Zoom AI Bot — FastAPI service running inside OrbStack amd64 VM.

Provides endpoints to join/leave Zoom meetings with an AI assistant.
The SDK runs in a separate subprocess (bot_process.py) because it
requires exclusive use of the main thread with a GLib event loop.

Run with: uvicorn zoom_bot.main:app --host 0.0.0.0 --port 8795
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (loaded from environment / .env)
# ---------------------------------------------------------------------------

_ENV_FILE = Path(__file__).parent / ".env"
_FALLBACK_ENV = Path("/opt/zoom-bot/.env")

STATE_FILE = "/tmp/zoom-bot-state.json"


def _load_env() -> None:
    """Load .env file into os.environ if keys are missing."""
    for env_path in (_ENV_FILE, _FALLBACK_ENV):
        if not env_path.is_file():
            continue
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value


_load_env()

DEFAULT_PROMPT = (
    "You are a friendly, helpful AI assistant who has joined a Zoom meeting. "
    "Have a natural conversation. Be concise — keep responses to 1-2 sentences. "
    "Listen carefully and respond naturally. "
    "If asked who you are, say you're an AI assistant here to help."
)

# Bot mode: "sdk" uses Zoom Meeting SDK (bot_process.py),
# "chrome" uses Playwright + Chromium (chrome_bot.py)
BOT_MODE = os.environ.get("ZOOM_BOT_MODE", "sdk")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class BotState(str, Enum):
    IDLE = "idle"
    JOINING = "joining"
    IN_MEETING = "in_meeting"
    LEAVING = "leaving"
    ERROR = "error"


class JoinRequest(BaseModel):
    meeting_id: str
    passcode: str = ""
    prompt: str = Field(default=DEFAULT_PROMPT)
    max_duration: int = Field(default=180, ge=30, le=7200)
    mode: str = Field(default="", description="Bot mode: 'sdk' or 'chrome'. Empty uses ZOOM_BOT_MODE env.")


class StatusResponse(BaseModel):
    state: BotState
    meeting_id: str | None = None
    uptime_seconds: float | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    status: str = "ok"
    state: BotState = BotState.IDLE


# ---------------------------------------------------------------------------
# Bot process management
# ---------------------------------------------------------------------------


class _BotManager:
    """Manages the bot subprocess."""

    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._meeting_id: str | None = None
        self._join_time: float | None = None

    def _read_state(self) -> dict:
        """Read state from the bot subprocess state file."""
        try:
            if os.path.isfile(STATE_FILE):
                with open(STATE_FILE) as f:
                    return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
        return {"state": "idle"}

    def _is_process_alive(self) -> bool:
        if self._process is None:
            return False
        return self._process.poll() is None

    def get_state(self) -> BotState:
        """Get current bot state."""
        if self._is_process_alive():
            state_data = self._read_state()
            state_str = state_data.get("state", "idle")
            try:
                return BotState(state_str)
            except ValueError:
                return BotState.IDLE
        else:
            # Process not running — clean up
            if self._process is not None:
                self._process = None
                self._meeting_id = None
                self._join_time = None
            return BotState.IDLE

    def get_status(self) -> StatusResponse:
        state = self.get_state()
        state_data = self._read_state()

        uptime = None
        if self._join_time and state in (BotState.IN_MEETING, BotState.JOINING):
            uptime = round(time.time() - self._join_time, 1)

        return StatusResponse(
            state=state,
            meeting_id=state_data.get("meeting_id") or self._meeting_id,
            uptime_seconds=uptime,
            error=state_data.get("error"),
        )

    def join(self, meeting_id: str, passcode: str, prompt: str, max_duration: int, mode: str = "") -> None:
        """Start the bot subprocess to join a meeting."""
        if self._is_process_alive():
            raise RuntimeError("Bot process already running")

        # Clean state file
        try:
            os.remove(STATE_FILE)
        except FileNotFoundError:
            pass

        self._meeting_id = meeting_id
        self._join_time = time.time()

        # Find the Python interpreter
        venv_python = "/opt/zoom-bot/venv/bin/python"
        if not os.path.isfile(venv_python):
            venv_python = "python3"

        # Select bot module based on mode
        bot_mode = mode or BOT_MODE
        if bot_mode == "chrome":
            bot_module = "zoom_bot.chrome_bot"
        else:
            bot_module = "zoom_bot.bot_process"

        cmd = [
            venv_python, "-m", bot_module,
            "--meeting-id", meeting_id,
            "--passcode", passcode,
            "--max-duration", str(max_duration),
            "--prompt", prompt,
        ]

        logger.info("Starting bot process (mode=%s): %s", bot_mode, " ".join(cmd[:6]))
        log_file = open("/tmp/zoom-bot-process.log", "w")
        self._process = subprocess.Popen(
            cmd,
            cwd="/opt/zoom-bot",
            env={**os.environ, "PYTHONPATH": "/opt/zoom-bot"},
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # new process group for clean killpg
        )

        # Wait briefly for the process to start and write initial state
        time.sleep(1)
        if not self._is_process_alive():
            # Process already died
            output = ""
            if self._process.stdout:
                output = self._process.stdout.read().decode(errors="replace")[:500]
            raise RuntimeError(f"Bot process exited immediately: {output}")

    def leave(self) -> None:
        """Signal the bot subprocess to leave, allowing clean browser shutdown."""
        if self._process and self._is_process_alive():
            # SIGTERM to main process only — let it close browser cleanly
            # (browser.close() sends WebRTC goodbye so Zoom drops the participant)
            self._process.send_signal(signal.SIGTERM)
            try:
                self._process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                # Clean shutdown failed — force-kill entire process group
                logger.warning("Bot process didn't exit in 15s, killing process group")
                try:
                    pgid = os.getpgid(self._process.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    self._process.kill()

        self._process = None
        self._meeting_id = None
        self._join_time = None

        # Clean state file
        try:
            os.remove(STATE_FILE)
        except FileNotFoundError:
            pass


_manager = _BotManager()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("zoom-bot service starting on port 8795")
    yield
    # Cleanup on shutdown
    if _manager.get_state() != BotState.IDLE:
        _manager.leave()
    logger.info("zoom-bot service stopped")


app = FastAPI(
    title="Zoom AI Bot",
    description="Native Zoom Meeting SDK bot with Deepgram STT + Groq LLM + ElevenLabs TTS",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(state=_manager.get_state())


@app.get("/status", response_model=StatusResponse)
async def status():
    return _manager.get_status()


@app.post("/join", response_model=StatusResponse)
async def join(req: JoinRequest):
    state = _manager.get_state()
    if state == BotState.IN_MEETING:
        raise HTTPException(400, "Already in a meeting. Leave first.")
    if state == BotState.JOINING:
        raise HTTPException(400, "Already joining a meeting.")

    # Validate credentials (chrome mode doesn't need Zoom SDK keys)
    bot_mode = req.mode or BOT_MODE
    if bot_mode == "chrome":
        required_keys = [
            "DEEPGRAM_API_KEY",
            "GROQ_API_KEY",
            "ELEVENLABS_API_KEY",
        ]
    else:
        required_keys = [
            "ZOOM_APP_CLIENT_ID",
            "ZOOM_APP_CLIENT_SECRET",
            "DEEPGRAM_API_KEY",
            "GROQ_API_KEY",
            "ELEVENLABS_API_KEY",
        ]
    missing = [k for k in required_keys if not os.environ.get(k)]
    if missing:
        raise HTTPException(500, f"Missing credentials: {', '.join(missing)}")

    try:
        _manager.join(req.meeting_id, req.passcode, req.prompt, req.max_duration, req.mode)
    except Exception as exc:
        logger.exception("Failed to start bot process")
        raise HTTPException(500, f"Failed to join meeting: {exc}")

    # Wait a moment for the bot to connect
    time.sleep(2)

    return _manager.get_status()


@app.post("/leave", response_model=StatusResponse)
async def leave():
    state = _manager.get_state()
    if state == BotState.IDLE:
        raise HTTPException(400, "Not in a meeting.")

    _manager.leave()

    return StatusResponse(state=BotState.IDLE)
