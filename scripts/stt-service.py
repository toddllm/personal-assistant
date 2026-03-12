#!/usr/bin/env python3
"""Lightweight faster-whisper STT service for GPU inference.

Run on a GPU machine (e.g. toddllm) alongside the TTS service:
    python scripts/stt-service.py --model medium.en --port 8791

Exposes:
    POST /v1/transcribe       — accepts raw 16kHz s16le mono PCM, returns JSON transcript
    GET  /v1/models           — list loaded models and estimated VRAM usage
    GET  /health              — health check
"""

import argparse
import logging
import threading
import time
from typing import Optional

import numpy as np
import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("stt-service")

app = FastAPI(title="Whisper STT Service")

# Model cache: model_name -> WhisperModel instance
_models: dict = {}
_default_model_name: str = ""
_model_lock = threading.Lock()

# Rough VRAM estimates per model (in GB) for float16 on CUDA
_VRAM_ESTIMATES: dict[str, float] = {
    "tiny": 1.0,
    "tiny.en": 1.0,
    "base": 1.0,
    "base.en": 1.0,
    "small": 2.0,
    "small.en": 2.0,
    "medium": 5.0,
    "medium.en": 5.0,
    "large-v1": 10.0,
    "large-v2": 10.0,
    "large-v3": 10.0,
    "large-v3-turbo": 10.0,
    "distil-large-v3": 6.0,
    "distil-medium.en": 4.0,
}


def _get_or_load_model(model_name: str):
    """Return a cached model or load it on first use. Thread-safe."""
    if model_name in _models:
        return _models[model_name]

    with _model_lock:
        # Double-check after acquiring lock
        if model_name in _models:
            return _models[model_name]

        from faster_whisper import WhisperModel
        logger.info("Loading faster-whisper model '%s' on CUDA...", model_name)
        t0 = time.time()
        model = WhisperModel(model_name, device="cuda", compute_type="float16")
        _models[model_name] = model
        logger.info("Model '%s' loaded in %.1fs", model_name, time.time() - t0)
        return model


def load_default_model(model_name: str):
    """Load the default model at startup and set it as the default."""
    global _default_model_name
    _default_model_name = model_name
    _get_or_load_model(model_name)


@app.get("/health")
async def health():
    loaded = list(_models.keys())
    return {
        "status": "ok",
        "default_model": _default_model_name,
        "loaded_models": loaded,
        "ready": len(_models) > 0,
    }


@app.get("/v1/models")
async def list_models():
    """List all currently loaded models and their estimated VRAM usage."""
    models = []
    for name in _models:
        models.append({
            "name": name,
            "vram_gb_estimate": _VRAM_ESTIMATES.get(name, -1),
            "is_default": name == _default_model_name,
        })
    total_vram = sum(
        _VRAM_ESTIMATES.get(name, 0) for name in _models
    )
    return {
        "models": models,
        "total_vram_gb_estimate": round(total_vram, 1),
    }


@app.post("/v1/transcribe")
async def transcribe(
    request: Request,
    model: Optional[str] = Query(None, description="Whisper model to use (e.g. large-v3). Defaults to startup model."),
):
    """Transcribe raw 16kHz s16le mono PCM audio.

    Send the PCM bytes directly as the request body.
    Content-Type should be application/octet-stream.

    Optional query parameter:
        model — which Whisper model to use (loaded on first request, cached afterwards).
    """
    model_name = model or _default_model_name
    if not model_name:
        return JSONResponse({"error": "no model specified and no default model loaded"}, status_code=503)

    body = await request.body()
    if len(body) < 3200:  # < 100ms
        return JSONResponse({"text": "", "duration_s": 0, "model": model_name})

    # Load / retrieve the model (may block briefly on first load)
    try:
        whisper_model = _get_or_load_model(model_name)
    except Exception as exc:
        logger.error("Failed to load model '%s': %s", model_name, exc)
        return JSONResponse(
            {"error": f"failed to load model '{model_name}': {exc}"},
            status_code=500,
        )

    audio = np.frombuffer(body, dtype=np.int16).astype(np.float32) / 32768.0
    duration_s = len(audio) / 16000

    t0 = time.time()
    segments, info = whisper_model.transcribe(
        audio,
        beam_size=1,
        language="en",
        vad_filter=True,
        no_speech_threshold=0.6,
    )
    parts = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            parts.append(text)
    transcript = " ".join(parts)
    elapsed = time.time() - t0

    logger.info(
        "[%s] Transcribed %.1fs audio in %.2fs: %s",
        model_name, duration_s, elapsed, transcript[:80],
    )
    return JSONResponse({
        "text": transcript,
        "duration_s": round(duration_s, 2),
        "inference_s": round(elapsed, 3),
        "model": model_name,
    })


def main():
    parser = argparse.ArgumentParser(description="Whisper STT Service")
    parser.add_argument("--model", default="medium.en", help="Default Whisper model loaded at startup")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_default_model(args.model)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
