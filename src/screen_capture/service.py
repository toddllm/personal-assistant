"""FastAPI application for the screen capture service."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from .capture import CaptureBackend, get_backend
from .config import Settings
from .schemas import (
    CaptureListResponse,
    CaptureRecord,
    CaptureRequest,
    DeleteResponse,
    ErrorResponse,
    HealthResponse,
)
from .storage import CaptureStore

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    backend: CaptureBackend | None = None,
    store: CaptureStore | None = None,
) -> FastAPI:
    """Create and configure the FastAPI app with DI for testing."""
    if settings is None:
        settings = Settings()

    app = FastAPI(title="Screen Capture Service", version="0.1.0")
    app.state.start_time = time.time()

    # Lazy-init backend and store
    _backend = backend
    _store = store

    def _get_backend() -> CaptureBackend:
        nonlocal _backend
        if _backend is None:
            _backend = get_backend()
        return _backend

    def _get_store() -> CaptureStore:
        nonlocal _store
        if _store is None:
            _store = CaptureStore(
                db_path=settings.database_path,
                capture_dir=settings.capture_dir,
            )
        return _store

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            uptime_seconds=round(time.time() - app.state.start_time, 2),
        )

    @app.post("/v1/capture", response_model=CaptureRecord, status_code=201)
    async def trigger_capture(req: CaptureRequest) -> Any:
        s = _get_store()

        # Check frame limit
        count = s.session_frame_count(req.session_id)
        if count >= settings.max_frames_per_session:
            return JSONResponse(
                status_code=429,
                content={"detail": f"Frame limit reached ({settings.max_frames_per_session})"},
            )

        try:
            b = _get_backend()
            png_bytes, width, height = b.capture()
        except (ImportError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail=str(exc))

        # Scale if requested
        quality = req.quality or settings.capture_quality
        scale = req.scale or settings.capture_scale
        fmt = settings.capture_format

        # Convert PNG to target format with quality/scale
        try:
            from PIL import Image
            import io

            img = Image.open(io.BytesIO(png_bytes))
            if scale < 1.0:
                new_size = (int(img.width * scale), int(img.height * scale))
                img = img.resize(new_size, Image.LANCZOS)
                width, height = new_size

            buf = io.BytesIO()
            save_kwargs: dict[str, Any] = {}
            if fmt == "webp":
                save_kwargs["quality"] = quality
            elif fmt == "jpeg":
                save_kwargs["quality"] = quality
            img.save(buf, format=fmt.upper(), **save_kwargs)
            image_data = buf.getvalue()
        except ImportError:
            # Fallback: save raw PNG
            image_data = png_bytes
            fmt = "png"

        record = s.save_capture(req.session_id, image_data, width, height, fmt=fmt)
        return record

    # IMPORTANT: /v1/captures/latest must be registered BEFORE /v1/captures/{session_id}
    @app.get("/v1/captures/latest", response_model=CaptureRecord)
    async def get_latest() -> Any:
        s = _get_store()
        record = s.get_latest()
        if record is None:
            raise HTTPException(status_code=404, detail="No captures exist")
        return record

    @app.get("/v1/captures/{session_id}", response_model=CaptureListResponse)
    async def list_session_captures(
        session_id: str,
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> CaptureListResponse:
        s = _get_store()
        captures, total = s.list_session(session_id, limit=limit, offset=offset)
        return CaptureListResponse(captures=captures, total=total)

    @app.delete("/v1/captures/{session_id}", response_model=DeleteResponse)
    async def delete_session_captures(session_id: str) -> Any:
        s = _get_store()
        deleted = s.delete_session(session_id)
        if deleted == 0:
            raise HTTPException(status_code=404, detail="Session not found")
        return DeleteResponse(deleted_count=deleted)

    return app
