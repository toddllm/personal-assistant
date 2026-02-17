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
from fastapi.responses import FileResponse, StreamingResponse

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
