from __future__ import annotations

import base64
import json
from io import BytesIO
import logging
from pathlib import Path
import re
from typing import Iterator
import wave
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from tts_service.config import Settings
from tts_service.engine import Qwen3CustomVoiceEngine, StubTTSEngine
from tts_service.schemas import (
    CloneVoiceRequest,
    HealthResponse,
    PhotoUploadRequest,
    SynthesizeRequest,
    SynthesizeResponse,
    UpdateProfileRequest,
    VoiceProfile,
    VoiceProfilesResponse,
    VoicesResponse,
)

logger = logging.getLogger(__name__)


def _wav_bytes_from_float32(wav_float32, sample_rate: int) -> bytes:  # noqa: ANN001
    import numpy as np

    wav_arr = np.asarray(wav_float32, dtype=np.float32)
    if wav_arr.ndim > 1:
        wav_arr = np.mean(wav_arr, axis=-1).astype(np.float32)
    wav_arr = np.clip(wav_arr, -1.0, 1.0)
    pcm16 = (wav_arr * 32767.0).astype(np.int16)

    buf = BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm16.tobytes())
    return buf.getvalue()


def _sse(event: str, payload: dict[str, object]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _split_text_for_stream(text: str, max_chars: int) -> list[str]:
    normalized = text.strip()
    if not normalized:
        return []

    # First split by sentence boundaries.
    sentence_parts = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+", normalized)
        if part and part.strip()
    ]
    if not sentence_parts:
        sentence_parts = [normalized]

    chunks: list[str] = []
    for sentence in sentence_parts:
        if len(sentence) <= max_chars:
            chunks.append(sentence)
            continue

        # Preserve all text while splitting oversized sentences by word groups.
        words = sentence.split()
        current: list[str] = []
        current_len = 0
        for word in words:
            extra = len(word) if current_len == 0 else len(word) + 1
            if current and current_len + extra > max_chars:
                chunks.append(" ".join(current))
                current = [word]
                current_len = len(word)
            else:
                current.append(word)
                current_len += extra
        if current:
            chunks.append(" ".join(current))
    return chunks


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="tts-service", version="0.1.0")
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )
    settings.output_dir.mkdir(parents=True, exist_ok=True)

    if settings.provider == "stub":
        engine = StubTTSEngine(settings)
    else:
        engine = Qwen3CustomVoiceEngine(settings)

    @app.on_event("startup")
    def startup() -> None:
        if settings.startup_load_model:
            try:
                engine.load()
            except Exception:  # noqa: BLE001
                logger.exception("TTS model startup load failed. Service will continue in degraded mode.")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(VOICE_CLONE_UI)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        status = engine.status()
        return HealthResponse(
            status="ok" if status["model_loaded"] else "degraded",
            provider=str(status["provider"]),
            model_loaded=bool(status["model_loaded"]),
            load_error=status["load_error"] if isinstance(status.get("load_error"), str) else None,
        )

    @app.get("/v1/voices", response_model=VoicesResponse)
    def voices() -> VoicesResponse:
        status = engine.status()
        return VoicesResponse(
            provider=str(status["provider"]),
            model_loaded=bool(status["model_loaded"]),
            load_error=status["load_error"] if isinstance(status.get("load_error"), str) else None,
            voices=engine.list_voices(),
        )

    @app.post("/v1/synthesize", response_model=SynthesizeResponse)
    def synthesize(req: SynthesizeRequest) -> SynthesizeResponse:
        try:
            result = engine.synthesize(
                text=req.text,
                voice=req.voice,
                language=req.language,
                instruct=req.instruct,
                max_new_tokens=req.max_new_tokens,
                temperature=req.temperature,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        wav_bytes = _wav_bytes_from_float32(result.wav, result.sample_rate)
        audio_b64 = base64.b64encode(wav_bytes).decode("ascii")

        audio_url = None
        if req.save_audio:
            file_name = f"{uuid4().hex}.wav"
            path = settings.output_dir / file_name
            path.write_bytes(wav_bytes)
            audio_url = f"/v1/audio/{file_name}"

        return SynthesizeResponse(
            provider=result.provider,
            voice=result.voice,
            language=result.language,
            sample_rate=result.sample_rate,
            audio_base64=audio_b64,
            audio_url=audio_url,
        )

    @app.post("/v1/synthesize/stream")
    def synthesize_stream(req: SynthesizeRequest) -> StreamingResponse:
        chunk_limit = max(120, min(settings.max_text_chars, 420))

        def event_iter() -> Iterator[bytes]:
            chunks = _split_text_for_stream(req.text, max_chars=chunk_limit)
            if not chunks:
                yield _sse("error", {"error": "Text is empty."})
                yield _sse("done", {"ok": False, "chunks": 0})
                return

            yield _sse("start", {"chunks": len(chunks)})
            for idx, chunk in enumerate(chunks, start=1):
                try:
                    result = engine.synthesize(
                        text=chunk,
                        voice=req.voice,
                        language=req.language,
                        instruct=req.instruct,
                        max_new_tokens=req.max_new_tokens,
                        temperature=req.temperature,
                    )
                except Exception as exc:  # noqa: BLE001
                    yield _sse(
                        "error",
                        {
                            "index": idx,
                            "chunks": len(chunks),
                            "error": str(exc),
                        },
                    )
                    yield _sse("done", {"ok": False, "chunks": idx - 1})
                    return

                wav_bytes = _wav_bytes_from_float32(result.wav, result.sample_rate)
                audio_b64 = base64.b64encode(wav_bytes).decode("ascii")
                yield _sse(
                    "chunk",
                    {
                        "index": idx,
                        "chunks": len(chunks),
                        "text": chunk,
                        "provider": result.provider,
                        "voice": result.voice,
                        "language": result.language,
                        "sample_rate": result.sample_rate,
                        "mime_type": "audio/wav",
                        "audio_base64": audio_b64,
                    },
                )
            yield _sse("done", {"ok": True, "chunks": len(chunks)})

        return StreamingResponse(
            event_iter(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/v1/audio/{file_name}")
    def serve_audio(file_name: str) -> FileResponse:
        safe_name = Path(file_name).name
        if safe_name != file_name or not safe_name.endswith(".wav"):
            raise HTTPException(status_code=400, detail="Invalid file name.")
        path = settings.output_dir / safe_name
        if not path.exists():
            raise HTTPException(status_code=404, detail="Audio file not found.")
        return FileResponse(path, media_type="audio/wav", filename=safe_name)

    # --- Voice Profile Endpoints ---

    @app.get("/v1/profiles", response_model=VoiceProfilesResponse)
    def list_profiles() -> VoiceProfilesResponse:
        if not hasattr(engine, "list_voices_detailed"):
            # Stub engine fallback
            return VoiceProfilesResponse(
                profiles=[
                    VoiceProfile(name=v, display_name=v, voice_type="builtin")
                    for v in engine.list_voices()
                ]
            )
        return VoiceProfilesResponse(
            profiles=[VoiceProfile(**v) for v in engine.list_voices_detailed()]
        )

    @app.post("/v1/profiles/clone")
    def clone_voice(req: CloneVoiceRequest) -> dict:
        if not hasattr(engine, "clone_voice"):
            raise HTTPException(status_code=501, detail="Voice cloning not available with stub engine.")
        try:
            ref_audio_bytes = base64.b64decode(req.ref_audio_base64)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid base64 audio: {exc}") from exc

        try:
            profile = engine.clone_voice(
                name=req.name,
                ref_audio_bytes=ref_audio_bytes,
                ref_text=req.ref_text,
                display_name=req.display_name,
                is_owner=req.is_owner,
                x_vector_only=req.x_vector_only,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"ok": True, "profile": profile}

    @app.delete("/v1/profiles/{name}")
    def delete_profile(name: str) -> dict:
        if not hasattr(engine, "remove_voice"):
            raise HTTPException(status_code=501, detail="Not available with stub engine.")
        try:
            removed = engine.remove_voice(name)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not removed:
            raise HTTPException(status_code=404, detail=f"Voice profile '{name}' not found.")
        return {"ok": True}

    @app.put("/v1/profiles/{name}")
    def update_profile(name: str, req: UpdateProfileRequest) -> dict:
        if not hasattr(engine, "profile_store"):
            raise HTTPException(status_code=501, detail="Not available with stub engine.")
        updates = {k: v for k, v in req.model_dump().items() if v is not None}
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update.")
        profile = engine.profile_store.update(name, **updates)
        if profile is None:
            raise HTTPException(status_code=404, detail=f"Voice profile '{name}' not found.")
        return {"ok": True, "profile": profile}

    @app.post("/v1/profiles/{name}/photo")
    def upload_photo(name: str, req: PhotoUploadRequest) -> dict:
        if not hasattr(engine, "profile_store"):
            raise HTTPException(status_code=501, detail="Not available with stub engine.")
        try:
            photo_bytes = base64.b64decode(req.photo_base64)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid base64 photo: {exc}") from exc
        # Detect format from magic bytes
        ext = ".jpg"
        if photo_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            ext = ".png"
        path = engine.profile_store.set_photo(name, photo_bytes, ext)
        if path is None:
            raise HTTPException(status_code=404, detail=f"Voice profile '{name}' not found.")
        return {"ok": True, "photo_path": path}

    @app.get("/v1/profiles/{name}/photo")
    def get_photo(name: str) -> FileResponse:
        if not hasattr(engine, "profile_store"):
            raise HTTPException(status_code=501, detail="Not available with stub engine.")
        photo_path = engine.profile_store.get_photo_path(name)
        if not photo_path or not Path(photo_path).exists():
            raise HTTPException(status_code=404, detail="No photo for this profile.")
        media = "image/png" if photo_path.endswith(".png") else "image/jpeg"
        return FileResponse(photo_path, media_type=media)

    @app.post("/v1/profiles/{name}/test")
    def test_voice(name: str) -> dict:
        test_text = "Hello, this is a test of my voice."
        try:
            result = engine.synthesize(text=test_text, voice=name)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        wav_bytes = _wav_bytes_from_float32(result.wav, result.sample_rate)
        audio_b64 = base64.b64encode(wav_bytes).decode("ascii")
        return {
            "voice": result.voice,
            "sample_rate": result.sample_rate,
            "audio_base64": audio_b64,
        }

    return app


# ---------------------------------------------------------------------------
# Voice Clone Web UI
# ---------------------------------------------------------------------------

VOICE_CLONE_UI = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Voice Profiles — TTS Service</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    background: #1a1a2e; color: #e0e0e0;
    min-height: 100vh; padding: 20px;
  }
  .container { max-width: 900px; margin: 0 auto; }
  h1 { font-size: 20px; font-weight: 600; color: #e94560; margin-bottom: 4px; }
  .subtitle { font-size: 11px; color: #555; margin-bottom: 24px; }

  .grid { display: grid; grid-template-columns: 1fr 360px; gap: 16px; }
  @media (max-width: 760px) { .grid { grid-template-columns: 1fr; } }

  .panel {
    background: #16213e; border: 1px solid #0f3460;
    border-radius: 8px; padding: 16px;
  }
  .panel-title {
    font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 1px; color: #0f3460; margin-bottom: 12px;
  }

  /* Voice list */
  .voice-list { max-height: 520px; overflow-y: auto; }
  .section-label {
    font-size: 10px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.5px; color: #e94560; margin: 10px 0 6px; padding-left: 2px;
  }
  .voice-row {
    display: flex; align-items: center; gap: 8px;
    padding: 8px 10px; border-radius: 6px; margin-bottom: 4px;
    transition: background 0.15s;
  }
  .voice-row:hover { background: rgba(15, 52, 96, 0.4); }
  .voice-row.owner { background: rgba(233, 69, 96, 0.06); border: 1px solid rgba(233, 69, 96, 0.15); }
  .voice-name { font-size: 13px; font-weight: 500; flex: 1; }
  .voice-tag {
    font-size: 9px; color: #888; background: rgba(15, 52, 96, 0.5);
    padding: 2px 6px; border-radius: 3px;
  }
  .voice-tag.owner { color: #e94560; background: rgba(233, 69, 96, 0.12); }
  .voice-actions { display: flex; gap: 4px; }

  /* Buttons */
  .btn {
    font-size: 10px; padding: 4px 10px;
    border: 1px solid #0f3460; border-radius: 4px;
    background: #1a1a2e; color: #e0e0e0;
    cursor: pointer; transition: all 0.15s; white-space: nowrap;
  }
  .btn:hover { background: #0f3460; border-color: #e94560; }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn.playing { border-color: #2196f3; color: #2196f3; }
  .btn-danger { border-color: #ff5252; color: #ff5252; background: rgba(255,82,82,0.08); }
  .btn-danger:hover { background: rgba(255,82,82,0.2); }
  .btn-primary {
    font-size: 12px; padding: 10px 20px; font-weight: 600;
    border: 1px solid #e94560; border-radius: 6px;
    background: rgba(233, 69, 96, 0.1); color: #e94560;
    cursor: pointer; transition: all 0.15s; width: 100%; margin-top: 8px;
  }
  .btn-primary:hover:not(:disabled) { background: rgba(233, 69, 96, 0.2); }
  .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; border-color: #555; color: #888; background: rgba(85,85,85,0.1); }

  /* Clone form */
  .form-group { margin-bottom: 12px; }
  .form-label {
    display: block; font-size: 10px; color: #888;
    text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px;
  }
  input[type=text], textarea {
    width: 100%; font-size: 12px; padding: 8px 10px;
    background: #1a1a2e; color: #e0e0e0;
    border: 1px solid #0f3460; border-radius: 4px; outline: none;
    font-family: inherit;
  }
  input:focus, textarea:focus { border-color: #e94560; }
  textarea { resize: vertical; min-height: 60px; }

  .file-row { display: flex; align-items: center; gap: 8px; }
  .file-btn {
    font-size: 11px; padding: 8px 14px;
    border: 1px dashed #0f3460; border-radius: 4px;
    background: transparent; color: #888;
    cursor: pointer; transition: all 0.15s; white-space: nowrap;
  }
  .file-btn:hover { border-color: #e94560; color: #e0e0e0; }
  .file-btn.has-file { border-style: solid; border-color: #00e676; color: #00e676; }
  .file-name { font-size: 10px; color: #555; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
  .file-info { font-size: 10px; color: #444; margin-top: 4px; }

  /* Record button */
  .record-row { display: flex; gap: 8px; margin-top: 8px; }
  .btn-record {
    font-size: 11px; padding: 6px 14px;
    border: 1px solid #ff5252; border-radius: 4px;
    background: rgba(255, 82, 82, 0.08); color: #ff5252;
    cursor: pointer; transition: all 0.15s;
  }
  .btn-record:hover { background: rgba(255, 82, 82, 0.2); }
  .btn-record.recording {
    border-color: #ff5252; color: #fff; background: #ff5252;
    animation: rec-pulse 1s ease-in-out infinite;
  }
  @keyframes rec-pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.6; } }
  .record-time { font-size: 11px; color: #888; line-height: 30px; font-variant-numeric: tabular-nums; }

  .checkbox-row { display: flex; align-items: center; gap: 6px; margin-bottom: 12px; }
  .checkbox-row label { font-size: 11px; color: #888; cursor: pointer; }

  .status-msg {
    font-size: 11px; min-height: 16px; margin-top: 8px; padding: 6px;
    border-radius: 4px; text-align: center;
  }
  .status-msg.ok { color: #00e676; background: rgba(0,230,118,0.08); }
  .status-msg.err { color: #ff5252; background: rgba(255,82,82,0.08); }
  .status-msg.info { color: #2196f3; background: rgba(33,150,243,0.08); }

  .empty-list { color: #444; font-size: 12px; font-style: italic; padding: 20px 0; text-align: center; }

  /* Edit form (inline) */
  .edit-form {
    padding: 10px; margin: 4px 0 8px; border-radius: 6px;
    background: rgba(15, 52, 96, 0.3); border: 1px solid #0f3460;
  }
  .edit-form .form-group { margin-bottom: 8px; }
  .edit-form .form-label { margin-bottom: 2px; }
  .edit-form input[type=text] { font-size: 12px; padding: 6px 8px; }
  .edit-form .edit-btn-row { display: flex; gap: 6px; margin-top: 8px; }
  .edit-form .btn-save {
    font-size: 11px; padding: 6px 14px; font-weight: 600;
    border: 1px solid #00e676; border-radius: 4px;
    background: rgba(0,230,118,0.1); color: #00e676;
    cursor: pointer; transition: all 0.15s;
  }
  .edit-form .btn-save:hover { background: rgba(0,230,118,0.2); }
  .edit-form .btn-cancel {
    font-size: 11px; padding: 6px 14px;
    border: 1px solid #555; border-radius: 4px;
    background: transparent; color: #888;
    cursor: pointer; transition: all 0.15s;
  }
  .edit-form .btn-cancel:hover { border-color: #888; color: #e0e0e0; }
  .btn-edit { border-color: #ffc107; color: #ffc107; background: rgba(255,193,7,0.08); }
  .btn-edit:hover { background: rgba(255,193,7,0.2); }

  /* Test text input */
  .test-row { margin-top: 12px; padding-top: 12px; border-top: 1px solid #0f3460; }
  .test-input-row { display: flex; gap: 6px; }
  .test-input-row input { flex: 1; }
  .test-input-row .btn { padding: 8px 14px; }

  /* Bot settings bar */
  .bot-bar {
    margin-top: 16px; display: grid; grid-template-columns: 1fr 1fr; gap: 16px;
  }
  @media (max-width: 760px) { .bot-bar { grid-template-columns: 1fr; } }
  .bot-card {
    background: #16213e; border: 1px solid #0f3460;
    border-radius: 8px; padding: 12px 16px;
  }
  .bot-card.full-width { grid-column: 1 / -1; }
  .bot-card-title {
    font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 1px; color: #0f3460; margin-bottom: 8px;
  }
  .bot-card-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
  .bot-card-row:last-child { margin-bottom: 0; }
  .bot-card-row select, .bot-card-row input[type=text] {
    flex: 1; font-size: 12px; padding: 6px 8px;
    background: #1a1a2e; color: #e0e0e0;
    border: 1px solid #0f3460; border-radius: 4px; outline: none;
  }
  .bot-card-row select:focus, .bot-card-row input:focus { border-color: #e94560; }
  .bot-status { font-size: 10px; color: #555; margin-top: 6px; }
  .bot-status.connected { color: #00e676; }
  .bot-status.offline { color: #ff5252; }

  /* Latency results */
  .latency-results {
    margin-top: 10px; max-height: 300px; overflow-y: auto;
  }
  .latency-row {
    display: flex; align-items: center; gap: 8px;
    padding: 6px 8px; border-radius: 4px; margin-bottom: 3px;
    font-size: 11px; background: rgba(15, 52, 96, 0.2);
  }
  .latency-voice { font-weight: 500; min-width: 80px; }
  .latency-time { color: #00e676; font-variant-numeric: tabular-nums; min-width: 60px; }
  .latency-time.slow { color: #ffc107; }
  .latency-time.very-slow { color: #ff5252; }
  .latency-bar {
    flex: 1; height: 6px; background: #1a1a2e; border-radius: 3px; overflow: hidden;
  }
  .latency-bar-fill { height: 100%; border-radius: 3px; transition: width 0.3s; }
  .latency-play { cursor: pointer; opacity: 0.6; transition: opacity 0.15s; }
  .latency-play:hover { opacity: 1; }
</style>
</head>
<body>
<div class="container">
  <h1>Voice Profiles</h1>
  <div class="subtitle">Manage voice clones &amp; test TTS voices</div>

  <div class="grid">
    <!-- Left: Voice List -->
    <div class="panel">
      <div class="panel-title">All Voices</div>
      <div class="voice-list" id="voice-list">
        <div class="empty-list">Loading voices...</div>
      </div>
      <div class="test-row">
        <label class="form-label">Test Any Voice</label>
        <div class="test-input-row">
          <input type="text" id="test-text" value="Hello, this is a test of my voice." placeholder="Enter text to speak..." />
          <button class="btn" id="btn-test-custom" disabled>Speak</button>
        </div>
      </div>
    </div>

    <!-- Right: Clone Form -->
    <div class="panel">
      <div class="panel-title">Clone New Voice</div>
      <div class="form-group">
        <label class="form-label">Voice ID (lowercase, a-z, 0-9, _)</label>
        <input type="text" id="clone-name" placeholder="e.g. todd" pattern="[a-z0-9_]+" maxlength="50" />
      </div>
      <div class="form-group">
        <label class="form-label">Display Name</label>
        <input type="text" id="clone-display" placeholder="e.g. Todd" maxlength="100" />
      </div>
      <div class="form-group">
        <label class="form-label">Reference Text (what's said in the audio)</label>
        <textarea id="clone-reftext" placeholder="Type the exact transcript of the reference audio..."></textarea>
      </div>
      <div class="form-group">
        <label class="form-label">Reference Audio (3-30 seconds WAV)</label>
        <div class="file-row">
          <button class="file-btn" id="btn-choose-file">Choose WAV File</button>
          <span class="file-name" id="file-name">no file selected</span>
          <input type="file" id="file-input" accept=".wav,audio/wav,audio/x-wav" style="display:none" />
        </div>
        <div class="file-info" id="file-info"></div>
        <div class="record-row">
          <button class="btn-record" id="btn-record">Record from Mic</button>
          <span class="record-time" id="record-time"></span>
        </div>
      </div>
      <div class="checkbox-row">
        <input type="checkbox" id="clone-owner" /> <label for="clone-owner">Mark as owner voice</label>
      </div>
      <button class="btn-primary" id="btn-clone" disabled>Clone Voice</button>
      <div class="status-msg" id="status-msg"></div>
    </div>
  </div>

  <!-- Bot Voice Settings & Testing -->
  <div class="bot-bar">
    <div class="bot-card">
      <div class="bot-card-title">Discord Bot Voice</div>
      <div class="bot-card-row">
        <select id="discord-voice"><option>loading...</option></select>
        <button class="btn" id="btn-discord-apply">Apply</button>
      </div>
      <div class="bot-status" id="discord-status">checking...</div>
    </div>

    <div class="bot-card">
      <div class="bot-card-title">Voice Latency Test</div>
      <div class="bot-card-row">
        <input type="text" id="bench-text" value="The quick brown fox jumps over the lazy dog." placeholder="Test phrase..." />
      </div>
      <div class="bot-card-row">
        <select id="bench-voice-select"><option value="__all__">All voices</option></select>
        <button class="btn" id="btn-bench">Run Test</button>
        <button class="btn" id="btn-bench-all">Benchmark All</button>
      </div>
      <div class="bot-status" id="bench-status"></div>
    </div>

    <div class="bot-card full-width">
      <div class="bot-card-title">Results</div>
      <div class="latency-results" id="latency-results">
        <div class="empty-list">Run a test to see latency results</div>
      </div>
    </div>
  </div>
</div>

<script>
const API = '';
let selectedVoice = null;
let audioCtx = null;
let mediaRecorder = null;
let recordedChunks = [];
let recordingStartTime = 0;
let recordTimer = null;
let recordedBlob = null;

// --- Init ---
async function init() {
  await loadVoices();
  setInterval(loadVoices, 30000);

  document.getElementById('btn-choose-file').onclick = () => document.getElementById('file-input').click();
  document.getElementById('file-input').onchange = onFileSelected;
  document.getElementById('btn-record').onclick = toggleRecording;
  document.getElementById('btn-clone').onclick = cloneVoice;
  document.getElementById('btn-test-custom').onclick = testCustomText;

  document.getElementById('clone-name').oninput = updateCloneBtn;
  document.getElementById('clone-display').oninput = updateCloneBtn;
  document.getElementById('clone-reftext').oninput = updateCloneBtn;

  // Discord bot voice
  document.getElementById('btn-discord-apply').onclick = applyDiscordVoice;
  document.getElementById('btn-bench').onclick = () => {
    const v = document.getElementById('bench-voice-select').value;
    if (v !== '__all__') document.getElementById('bench-voice-select').value = v;
    runBenchmark();
  };
  document.getElementById('btn-bench-all').onclick = () => {
    document.getElementById('bench-voice-select').value = '__all__';
    runBenchmark();
  };
  loadDiscordVoice();
  setInterval(loadDiscordVoice, 30000);
}

// --- Voice List ---
async function loadVoices() {
  try {
    const r = await fetch(API + '/v1/profiles');
    const d = await r.json();
    renderVoices(d.profiles);
  } catch (e) {
    document.getElementById('voice-list').innerHTML = '<div class="empty-list">Service unavailable</div>';
  }
}

function renderVoices(profiles) {
  const el = document.getElementById('voice-list');
  const cloned = profiles.filter(p => p.voice_type === 'cloned');
  const builtin = profiles.filter(p => p.voice_type === 'builtin');
  let html = '';

  if (cloned.length > 0) {
    html += '<div class="section-label">Cloned Voices</div>';
    cloned.forEach(p => {
      const isOwner = p.is_owner;
      const sel = selectedVoice === p.name ? ' style="outline:1px solid #e94560"' : '';
      html += `<div class="voice-row${isOwner ? ' owner' : ''}"${sel} data-voice="${esc(p.name)}">
        <span class="voice-name">${esc(p.display_name)}</span>
        ${isOwner ? '<span class="voice-tag owner">owner</span>' : '<span class="voice-tag">cloned</span>'}
        <div class="voice-actions">
          <button class="btn" onclick="testVoice('${esc(p.name)}', this)">Test</button>
          <button class="btn btn-edit" onclick="showEdit('${esc(p.name)}', '${esc(p.display_name)}', ${p.is_owner})">Edit</button>
          <button class="btn btn-danger" onclick="deleteVoice('${esc(p.name)}')">Delete</button>
        </div>
      </div>
      <div class="edit-form" id="edit-${esc(p.name)}" style="display:none">
        <div class="form-group">
          <label class="form-label">Display Name</label>
          <input type="text" id="edit-display-${esc(p.name)}" value="${esc(p.display_name)}" maxlength="100" />
        </div>
        <div class="checkbox-row">
          <input type="checkbox" id="edit-owner-${esc(p.name)}" ${p.is_owner ? 'checked' : ''} />
          <label for="edit-owner-${esc(p.name)}">Owner voice</label>
        </div>
        <div class="edit-btn-row">
          <button class="btn-save" onclick="saveEdit('${esc(p.name)}')">Save</button>
          <button class="btn-cancel" onclick="hideEdit('${esc(p.name)}')">Cancel</button>
        </div>
      </div>`;
    });
  }

  html += '<div class="section-label">Builtin Voices</div>';
  builtin.forEach(p => {
    html += `<div class="voice-row" data-voice="${esc(p.name)}">
      <span class="voice-name">${esc(p.display_name)}</span>
      <span class="voice-tag">builtin</span>
      <div class="voice-actions">
        <button class="btn" onclick="testVoice('${esc(p.name)}', this)">Test</button>
      </div>
    </div>`;
  });

  el.innerHTML = html;

  // Click to select voice for custom test
  el.querySelectorAll('.voice-row').forEach(row => {
    row.style.cursor = 'pointer';
    row.onclick = (e) => {
      if (e.target.tagName === 'BUTTON') return;
      selectedVoice = row.dataset.voice;
      document.getElementById('btn-test-custom').disabled = false;
      el.querySelectorAll('.voice-row').forEach(r => r.style.outline = '');
      row.style.outline = '1px solid #e94560';
    };
  });
}

// --- Test Voice ---
let currentAudio = null;
async function testVoice(name, btn) {
  if (currentAudio) { currentAudio.pause(); currentAudio = null; }
  const origText = btn.textContent;
  btn.textContent = '...';
  btn.classList.add('playing');
  try {
    const r = await fetch(API + '/v1/profiles/' + encodeURIComponent(name) + '/test', { method: 'POST' });
    const d = await r.json();
    if (d.audio_base64) {
      currentAudio = new Audio('data:audio/wav;base64,' + d.audio_base64);
      currentAudio.onended = () => { btn.textContent = origText; btn.classList.remove('playing'); currentAudio = null; };
      currentAudio.play();
    } else {
      btn.textContent = origText; btn.classList.remove('playing');
    }
  } catch (e) {
    btn.textContent = origText; btn.classList.remove('playing');
    console.error(e);
  }
}

async function testCustomText() {
  if (!selectedVoice) return;
  const text = document.getElementById('test-text').value.trim();
  if (!text) return;
  if (currentAudio) { currentAudio.pause(); currentAudio = null; }
  const btn = document.getElementById('btn-test-custom');
  btn.disabled = true; btn.textContent = '...';
  try {
    const r = await fetch(API + '/v1/synthesize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, voice: selectedVoice, save_audio: false }),
    });
    const d = await r.json();
    if (d.audio_base64) {
      currentAudio = new Audio('data:audio/wav;base64,' + d.audio_base64);
      currentAudio.onended = () => { btn.textContent = 'Speak'; btn.disabled = false; currentAudio = null; };
      btn.textContent = 'Playing...';
      currentAudio.play();
    } else {
      btn.textContent = 'Speak'; btn.disabled = false;
      showStatus(d.detail || 'No audio returned', 'err');
    }
  } catch (e) {
    btn.textContent = 'Speak'; btn.disabled = false;
    showStatus('Error: ' + e.message, 'err');
  }
}

// --- Delete Voice ---
async function deleteVoice(name) {
  if (!confirm('Delete cloned voice "' + name + '"?')) return;
  try {
    const r = await fetch(API + '/v1/profiles/' + encodeURIComponent(name), { method: 'DELETE' });
    const d = await r.json();
    if (d.ok) { showStatus('Deleted "' + name + '"', 'ok'); loadVoices(); }
    else showStatus(d.detail || 'Delete failed', 'err');
  } catch (e) { showStatus('Error: ' + e.message, 'err'); }
}

// --- File Selection ---
let selectedFile = null;
function onFileSelected() {
  const input = document.getElementById('file-input');
  const file = input.files[0];
  if (!file) return;
  selectedFile = file;
  recordedBlob = null;
  document.getElementById('file-name').textContent = file.name;
  document.getElementById('btn-choose-file').className = 'file-btn has-file';

  // Show duration info
  const url = URL.createObjectURL(file);
  const audio = new Audio(url);
  audio.onloadedmetadata = () => {
    document.getElementById('file-info').textContent =
      file.name + ' — ' + audio.duration.toFixed(1) + 's, ' + (file.size / 1024).toFixed(0) + ' KB';
    URL.revokeObjectURL(url);
  };
  updateCloneBtn();
}

// --- Recording ---
async function toggleRecording() {
  const btn = document.getElementById('btn-record');
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    mediaRecorder.stop();
    btn.textContent = 'Record from Mic';
    btn.classList.remove('recording');
    clearInterval(recordTimer);
    return;
  }

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    recordedChunks = [];
    mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm;codecs=opus' });
    mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) recordedChunks.push(e.data); };
    mediaRecorder.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      // Convert webm to wav via AudioContext
      const webmBlob = new Blob(recordedChunks, { type: 'audio/webm' });
      try {
        const arrayBuf = await webmBlob.arrayBuffer();
        if (!audioCtx) audioCtx = new AudioContext({ sampleRate: 24000 });
        const decoded = await audioCtx.decodeAudioData(arrayBuf);
        const wavBlob = audioBufferToWav(decoded);
        recordedBlob = wavBlob;
        selectedFile = null;
        document.getElementById('file-input').value = '';
        document.getElementById('file-name').textContent = 'recorded audio';
        document.getElementById('btn-choose-file').className = 'file-btn has-file';
        const dur = decoded.duration.toFixed(1);
        document.getElementById('file-info').textContent = 'Recorded ' + dur + 's — ' + (wavBlob.size / 1024).toFixed(0) + ' KB';
        updateCloneBtn();
      } catch (e) { showStatus('Failed to convert recording: ' + e.message, 'err'); }
    };
    mediaRecorder.start(100);
    recordingStartTime = Date.now();
    btn.textContent = 'Stop Recording';
    btn.classList.add('recording');
    recordTimer = setInterval(() => {
      const s = ((Date.now() - recordingStartTime) / 1000).toFixed(1);
      document.getElementById('record-time').textContent = s + 's';
    }, 100);
  } catch (e) {
    showStatus('Mic access denied: ' + e.message, 'err');
  }
}

function audioBufferToWav(buffer) {
  const numCh = 1;
  const sr = buffer.sampleRate;
  const data = buffer.getChannelData(0);
  const bitsPerSample = 16;
  const byteRate = sr * numCh * bitsPerSample / 8;
  const blockAlign = numCh * bitsPerSample / 8;
  const dataSize = data.length * blockAlign;
  const buf = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buf);

  function writeStr(off, str) { for (let i = 0; i < str.length; i++) view.setUint8(off + i, str.charCodeAt(i)); }
  writeStr(0, 'RIFF');
  view.setUint32(4, 36 + dataSize, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, numCh, true);
  view.setUint32(24, sr, true);
  view.setUint32(28, byteRate, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, bitsPerSample, true);
  writeStr(36, 'data');
  view.setUint32(40, dataSize, true);

  let offset = 44;
  for (let i = 0; i < data.length; i++) {
    const s = Math.max(-1, Math.min(1, data[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
    offset += 2;
  }
  return new Blob([buf], { type: 'audio/wav' });
}

// --- Clone ---
function updateCloneBtn() {
  const name = document.getElementById('clone-name').value.trim();
  const display = document.getElementById('clone-display').value.trim();
  const reftext = document.getElementById('clone-reftext').value.trim();
  const hasAudio = selectedFile || recordedBlob;
  document.getElementById('btn-clone').disabled = !(name && display && reftext && hasAudio);
}

async function cloneVoice() {
  const name = document.getElementById('clone-name').value.trim();
  const display = document.getElementById('clone-display').value.trim();
  const reftext = document.getElementById('clone-reftext').value.trim();
  const isOwner = document.getElementById('clone-owner').checked;
  const audioBlob = recordedBlob || selectedFile;
  if (!name || !display || !reftext || !audioBlob) return;

  const btn = document.getElementById('btn-clone');
  btn.disabled = true; btn.textContent = 'Cloning...';
  showStatus('Uploading audio and computing voice embedding...', 'info');

  try {
    const arrayBuf = await audioBlob.arrayBuffer();
    const bytes = new Uint8Array(arrayBuf);
    let binary = '';
    for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
    const audioB64 = btoa(binary);

    const r = await fetch(API + '/v1/profiles/clone', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name, display_name: display, ref_text: reftext,
        ref_audio_base64: audioB64, is_owner: isOwner,
      }),
    });
    const d = await r.json();
    if (d.ok) {
      showStatus('Voice "' + display + '" cloned successfully!', 'ok');
      document.getElementById('clone-name').value = '';
      document.getElementById('clone-display').value = '';
      document.getElementById('clone-reftext').value = '';
      document.getElementById('clone-owner').checked = false;
      document.getElementById('file-input').value = '';
      document.getElementById('file-name').textContent = 'no file selected';
      document.getElementById('file-info').textContent = '';
      document.getElementById('btn-choose-file').className = 'file-btn';
      selectedFile = null; recordedBlob = null;
      updateCloneBtn();
      await loadVoices();
    } else {
      showStatus(d.detail || 'Clone failed', 'err');
    }
  } catch (e) {
    showStatus('Error: ' + e.message, 'err');
  }
  btn.disabled = false; btn.textContent = 'Clone Voice';
}

// --- Helpers ---
function showStatus(msg, type) {
  const el = document.getElementById('status-msg');
  el.textContent = msg;
  el.className = 'status-msg ' + (type || '');
  if (type === 'ok') setTimeout(() => { if (el.textContent === msg) el.textContent = ''; }, 5000);
}
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

// --- Discord Bot Voice ---
const DISCORD_API = 'http://localhost:8796';
let benchResults = [];

async function loadDiscordVoice() {
  const sel = document.getElementById('discord-voice');
  const benchSel = document.getElementById('bench-voice-select');
  const statusEl = document.getElementById('discord-status');

  // Helper to populate a select from voice objects
  function populateSelect(selectEl, voices, currentVoice, includeAll) {
    selectEl.innerHTML = '';
    if (includeAll) {
      const allOpt = document.createElement('option');
      allOpt.value = '__all__'; allOpt.textContent = 'All voices';
      selectEl.appendChild(allOpt);
    }
    const cloned = voices.filter(v => v.voice_type === 'cloned');
    const builtin = voices.filter(v => v.voice_type !== 'cloned');
    if (cloned.length > 0) {
      const grp = document.createElement('optgroup');
      grp.label = 'Cloned';
      cloned.forEach(v => {
        const opt = document.createElement('option');
        opt.value = v.name;
        opt.textContent = v.display_name + (v.is_owner ? ' (owner)' : '');
        if (v.name === currentVoice) opt.selected = true;
        grp.appendChild(opt);
      });
      selectEl.appendChild(grp);
    }
    if (builtin.length > 0) {
      const grp = document.createElement('optgroup');
      grp.label = 'Builtin';
      builtin.forEach(v => {
        const opt = document.createElement('option');
        opt.value = v.name;
        opt.textContent = v.display_name;
        if (v.name === currentVoice) opt.selected = true;
        grp.appendChild(opt);
      });
      selectEl.appendChild(grp);
    }
  }

  try {
    const r = await fetch(DISCORD_API + '/voice');
    if (!r.ok) throw new Error('status ' + r.status);
    const d = await r.json();
    const voices = d.available_voices || [];
    if (voices.length === 0) {
      sel.innerHTML = '<option value="">no voices available</option>';
    } else {
      populateSelect(sel, voices, d.voice, false);
      populateSelect(benchSel, voices, null, true);
    }
    // Show display name for current voice
    const currentProfile = voices.find(v => v.name === d.voice);
    const currentLabel = currentProfile ? currentProfile.display_name : d.voice;
    statusEl.textContent = 'connected — current: ' + currentLabel;
    statusEl.className = 'bot-status connected';
  } catch (e) {
    sel.innerHTML = '<option value="">offline</option>';
    statusEl.textContent = 'offline — is discord-bot running on :8796?';
    statusEl.className = 'bot-status offline';
    // Still load voices from TTS for benchmark
    try {
      const r2 = await fetch(API + '/v1/profiles');
      const d2 = await r2.json();
      const voices = (d2.profiles || []).map(p => ({
        name: p.name, display_name: p.display_name || p.name,
        voice_type: p.voice_type || 'builtin', is_owner: p.is_owner || false,
      }));
      populateSelect(benchSel, voices, null, true);
    } catch (_) {}
  }
}

async function applyDiscordVoice() {
  const sel = document.getElementById('discord-voice');
  const voice = sel.value;
  if (!voice) return;
  const statusEl = document.getElementById('discord-status');
  try {
    const r = await fetch(DISCORD_API + '/voice', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ voice }),
    });
    const d = await r.json();
    if (d.voice) {
      statusEl.textContent = 'voice set to: ' + d.voice;
      statusEl.className = 'bot-status connected';
      showStatus('Discord bot voice changed to "' + d.voice + '"', 'ok');
    } else {
      statusEl.textContent = d.detail || 'update failed';
      statusEl.className = 'bot-status offline';
    }
  } catch (e) {
    statusEl.textContent = 'error: ' + e.message;
    statusEl.className = 'bot-status offline';
  }
}

// --- Latency Benchmarking ---
async function benchmarkVoice(voice, text) {
  const t0 = performance.now();
  const r = await fetch(API + '/v1/synthesize', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, voice, save_audio: false }),
  });
  const d = await r.json();
  const latencyMs = performance.now() - t0;
  const audioSize = d.audio_base64 ? Math.round(d.audio_base64.length * 3 / 4 / 1024) : 0;
  return { voice, latencyMs, audioSize, audioBase64: d.audio_base64, provider: d.provider || '' };
}

async function runBenchmark() {
  const text = document.getElementById('bench-text').value.trim();
  if (!text) { showStatus('Enter test text', 'err'); return; }
  const voiceSel = document.getElementById('bench-voice-select').value;
  const statusEl = document.getElementById('bench-status');
  const btnSingle = document.getElementById('btn-bench');
  const btnAll = document.getElementById('btn-bench-all');
  btnSingle.disabled = true; btnAll.disabled = true;

  let voices = [];
  if (voiceSel === '__all__') {
    const opts = document.getElementById('bench-voice-select').options;
    for (let i = 1; i < opts.length; i++) voices.push(opts[i].value);
  } else {
    voices = [voiceSel];
  }

  benchResults = [];
  statusEl.textContent = 'Testing ' + voices.length + ' voice(s)...';
  statusEl.className = 'bot-status connected';

  for (let i = 0; i < voices.length; i++) {
    statusEl.textContent = 'Testing ' + voices[i] + ' (' + (i + 1) + '/' + voices.length + ')...';
    try {
      const result = await benchmarkVoice(voices[i], text);
      benchResults.push(result);
      renderBenchResults();
    } catch (e) {
      benchResults.push({ voice: voices[i], latencyMs: -1, audioSize: 0, audioBase64: null, error: e.message });
      renderBenchResults();
    }
  }

  statusEl.textContent = 'Done — ' + voices.length + ' voice(s) tested';
  btnSingle.disabled = false; btnAll.disabled = false;
}

function renderBenchResults() {
  const el = document.getElementById('latency-results');
  if (benchResults.length === 0) {
    el.innerHTML = '<div class="empty-list">Run a test to see latency results</div>';
    return;
  }

  const maxMs = Math.max(...benchResults.filter(r => r.latencyMs > 0).map(r => r.latencyMs), 1);
  let html = '';
  const sorted = [...benchResults].sort((a, b) => {
    if (a.latencyMs < 0) return 1;
    if (b.latencyMs < 0) return -1;
    return a.latencyMs - b.latencyMs;
  });

  sorted.forEach((r, i) => {
    if (r.latencyMs < 0) {
      html += '<div class="latency-row"><span class="latency-voice">' + esc(r.voice) + '</span><span class="latency-time very-slow">error</span><span style="font-size:10px;color:#ff5252">' + esc(r.error || 'failed') + '</span></div>';
      return;
    }
    const ms = Math.round(r.latencyMs);
    const pct = Math.round((r.latencyMs / maxMs) * 100);
    const cls = ms > 5000 ? 'very-slow' : ms > 2000 ? 'slow' : '';
    const color = ms > 5000 ? '#ff5252' : ms > 2000 ? '#ffc107' : '#00e676';
    const provTag = r.provider.includes('clone') ? '<span class="voice-tag">clone</span>' : '';
    html += '<div class="latency-row">' +
      '<span class="latency-voice">' + esc(r.voice) + ' ' + provTag + '</span>' +
      '<span class="latency-time ' + cls + '">' + ms + 'ms</span>' +
      '<div class="latency-bar"><div class="latency-bar-fill" style="width:' + pct + '%;background:' + color + '"></div></div>' +
      '<span style="font-size:10px;color:#555">' + r.audioSize + 'KB</span>' +
      (r.audioBase64 ? '<span class="latency-play" onclick="playBenchAudio(' + i + ')" title="Play">&#9654;</span>' : '') +
      '</div>';
  });
  el.innerHTML = html;
}

let benchAudio = null;
function playBenchAudio(idx) {
  if (benchAudio) { benchAudio.pause(); benchAudio = null; }
  const sorted = [...benchResults].sort((a, b) => {
    if (a.latencyMs < 0) return 1;
    if (b.latencyMs < 0) return -1;
    return a.latencyMs - b.latencyMs;
  });
  const r = sorted[idx];
  if (!r || !r.audioBase64) return;
  benchAudio = new Audio('data:audio/wav;base64,' + r.audioBase64);
  benchAudio.onended = () => { benchAudio = null; };
  benchAudio.play();
}

// --- Edit Voice ---
function showEdit(name) {
  const el = document.getElementById('edit-' + name);
  if (el) el.style.display = 'block';
}
function hideEdit(name) {
  const el = document.getElementById('edit-' + name);
  if (el) el.style.display = 'none';
}
async function saveEdit(name) {
  const displayEl = document.getElementById('edit-display-' + name);
  const ownerEl = document.getElementById('edit-owner-' + name);
  if (!displayEl) return;
  const displayName = displayEl.value.trim();
  if (!displayName) { showStatus('Display name cannot be empty', 'err'); return; }
  const isOwner = ownerEl ? ownerEl.checked : false;
  try {
    const r = await fetch(API + '/v1/profiles/' + encodeURIComponent(name), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: displayName, is_owner: isOwner }),
    });
    const d = await r.json();
    if (d.ok) {
      showStatus('Updated "' + name + '"', 'ok');
      await loadVoices();
    } else {
      showStatus(d.detail || 'Update failed', 'err');
    }
  } catch (e) { showStatus('Error: ' + e.message, 'err'); }
}

init();
</script>
</body>
</html>
"""
