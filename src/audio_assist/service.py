from __future__ import annotations

import base64
from datetime import UTC, datetime
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx
import numpy as np

from audio_assist.archive import AudioArchiver
from audio_assist.config import Settings
from audio_assist.diarization import SpeakerDiarizationClient
from audio_assist.levels import AudioLevelTracker
from audio_assist.postprocess import TranscriptPostProcessor
from audio_assist.qa import QAEngine
from audio_assist.schemas import (
    PCMIngestRequest,
    QueryAnswer,
    QueryRequest,
    SourceAudioLevel,
    SourceStartRequest,
    SourceStatus,
    TTSSynthesizeRequest,
    TranscriptPage,
    TranscriptIngestRequest,
    TranscriptItem,
    TranscriptPostprocessRequest,
    TranscriptPostprocessResult,
)
from audio_assist.sources import SourceManager
from audio_assist.storage import SessionRecord, TranscriptRecord, TranscriptStore
from audio_assist.transcriber import AudioSegment, TranscriptionWorker
from audio_assist.tts_client import TTSClient

logger = logging.getLogger(__name__)

SUMMARY_QUERY_HINTS = (
    "summarize",
    "summary",
    "recap",
    "what happened",
    "key points",
    "takeaways",
    "tl;dr",
    "tldr",
)


def _dbfs_from_pcm16(raw_pcm: bytes) -> tuple[float, float]:
    samples = np.frombuffer(raw_pcm, dtype=np.int16)
    if samples.size == 0:
        return (-96.0, -96.0)
    samples_f = samples.astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(samples_f * samples_f)))
    peak = float(np.max(np.abs(samples_f)))
    level_dbfs = -96.0 if rms <= 1e-9 else float(max(-96.0, 20.0 * np.log10(rms)))
    peak_dbfs = -96.0 if peak <= 1e-9 else float(max(-96.0, 20.0 * np.log10(peak)))
    return level_dbfs, peak_dbfs


def _is_summary_question(question: str) -> bool:
    normalized = question.strip().lower()
    if not normalized:
        return False
    if normalized in {"summary", "summarize", "recap", "tldr", "tl;dr"}:
        return True
    return any(hint in normalized for hint in SUMMARY_QUERY_HINTS)


def _probe_input_level(
    sd: object,
    *,
    device_index: int,
    device_name: str,
    max_input_channels: int,
    sample_rate: int,
    channels: int,
    duration_ms: int,
) -> dict[str, object]:
    channels = max(1, min(channels, max_input_channels))
    frames = max(256, int(sample_rate * (duration_ms / 1000.0)))
    try:
        with sd.RawInputStream(  # type: ignore[attr-defined]
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            device=device_index,
            blocksize=frames,
        ) as stream:
            raw_pcm, overflowed = stream.read(frames)
            if overflowed:
                logger.debug("Input probe overflow for device index=%s name=%s", device_index, device_name)
        level_dbfs, peak_dbfs = _dbfs_from_pcm16(bytes(raw_pcm))
        silent = level_dbfs <= -60.0
        return {
            "index": device_index,
            "name": device_name,
            "max_input_channels": max_input_channels,
            "level_dbfs": level_dbfs,
            "peak_dbfs": peak_dbfs,
            "silent": silent,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "index": device_index,
            "name": device_name,
            "max_input_channels": max_input_channels,
            "level_dbfs": -96.0,
            "peak_dbfs": -96.0,
            "silent": True,
            "error": str(exc),
        }


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="audio-assist", version="0.1.0")
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    store = TranscriptStore(settings.database_path)
    archiver = (
        AudioArchiver(settings.archive_audio_dir, channels=settings.channels)
        if settings.archive_audio
        else None
    )
    speaker_client = SpeakerDiarizationClient(
        enabled=settings.speaker_enabled,
        base_url=settings.speaker_service_url,
        timeout_seconds=settings.speaker_timeout_seconds,
        cooldown_seconds=settings.speaker_cooldown_seconds,
    )
    transcriber = TranscriptionWorker(
        store=store,
        model_name=settings.whisper_model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
        language=settings.whisper_language,
        archiver=archiver,
        speaker_client=speaker_client,
        mic_source_id=settings.mic_source_id,
        mic_speaker_name=settings.mic_speaker_name,
        speaker_async_enrichment=settings.speaker_async_enrichment,
        speaker_min_confidence=settings.speaker_min_confidence,
        speaker_queue_size=settings.speaker_queue_size,
        speaker_backfill_enabled=settings.speaker_backfill_enabled,
        speaker_backfill_interval_seconds=settings.speaker_backfill_interval_seconds,
        speaker_backfill_batch_size=settings.speaker_backfill_batch_size,
        speaker_backfill_since_seconds=settings.speaker_backfill_since_seconds,
    )
    level_tracker = AudioLevelTracker()

    def handle_segment(segment: AudioSegment) -> None:
        level_tracker.ingest(segment)
        transcriber.enqueue(segment)

    def item_from_record(record: TranscriptRecord) -> TranscriptItem:
        return TranscriptItem(
            id=record.id,
            source_id=record.source_id,
            session_id=record.session_id,
            started_at=record.started_at,
            ended_at=record.ended_at,
            text=record.text,
            speaker=record.speaker,
        )

    def item_from_session(session: SessionRecord) -> TranscriptItem:
        return TranscriptItem(
            id=session.id,
            source_id=session.source_id,
            session_id=session.session_id,
            started_at=session.started_at,
            ended_at=session.ended_at,
            text=session.text,
            speaker=session.speaker,
        )

    source_manager = SourceManager(
        on_segment=handle_segment,
        sample_rate=settings.sample_rate,
        channels=settings.channels,
        segment_seconds=settings.segment_seconds,
    )
    qa_engine = QAEngine(
        provider=settings.qa_provider,
        ollama_url=settings.ollama_url,
        ollama_model=settings.ollama_model,
        timeout_seconds=settings.ollama_timeout_seconds,
    )
    tts_client = TTSClient(
        enabled=settings.tts_enabled,
        base_url=settings.tts_service_url,
        timeout_seconds=settings.tts_timeout_seconds,
        default_voice=settings.tts_default_voice,
        default_language=settings.tts_default_language,
        default_instruct=settings.tts_default_instruct,
    )
    postprocessor = (
        TranscriptPostProcessor(
            store=store,
            archiver=archiver,
            default_model_name=settings.whisper_postprocess_model,
            default_device=settings.whisper_postprocess_device,
            default_compute_type=settings.whisper_postprocess_compute_type,
            default_language=settings.whisper_postprocess_language,
        )
        if archiver is not None
        else None
    )

    @app.on_event("startup")
    def startup() -> None:
        logger.info("Starting transcription worker...")
        transcriber.start()
        logger.info("audio-assist started.")

    @app.on_event("shutdown")
    def shutdown() -> None:
        source_manager.stop_all()
        transcriber.stop()
        store.close()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/transcripts")
    def transcripts_page() -> FileResponse:
        return FileResponse(static_dir / "transcripts.html")

    @app.get("/v1/sources", response_model=list[SourceStatus])
    def list_sources() -> list[SourceStatus]:
        statuses = []
        for runtime in source_manager.statuses():
            statuses.append(
                SourceStatus(
                    source_id=runtime.source_id,
                    source_type=runtime.source_type,
                    running=runtime.runner.is_running(),
                    started_at=runtime.started_at,
                    details={},
                )
            )
        return statuses

    @app.get("/v1/sources/levels", response_model=list[SourceAudioLevel])
    def source_levels() -> list[SourceAudioLevel]:
        snapshots = level_tracker.snapshot()
        return [
            SourceAudioLevel(
                source_id=item.source_id,
                level_dbfs=item.level_dbfs,
                peak_dbfs=item.peak_dbfs,
                updated_at=item.updated_at,
                age_seconds=item.age_seconds,
                silent=item.silent,
                clipped=item.clipped,
            )
            for item in snapshots
        ]

    @app.get("/v1/ollama/models")
    def list_ollama_models() -> dict[str, object]:
        base_url = settings.ollama_url.rstrip("/")
        try:
            with httpx.Client(timeout=4.0) as client:
                resp = client.get(f"{base_url}/api/tags")
                resp.raise_for_status()
                payload = resp.json()
            names: list[str] = []
            for model in payload.get("models", []):
                name = str(model.get("name", "")).strip()
                if name:
                    names.append(name)
            if not names:
                names = [settings.ollama_model]
            return {
                "models": names,
                "default_model": settings.ollama_model,
                "reachable": True,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "models": [settings.ollama_model],
                "default_model": settings.ollama_model,
                "reachable": False,
                "error": str(exc),
            }

    @app.get("/v1/speaker/status")
    def speaker_status() -> dict[str, object]:
        payload = speaker_client.status()
        payload["pipeline"] = transcriber.speaker_pipeline_status()
        return payload

    @app.post("/v1/speaker/backfill")
    def speaker_backfill(limit: int | None = None, since_seconds: int | None = None) -> dict[str, object]:
        result = transcriber.run_speaker_backfill_once(limit=limit, since_seconds=since_seconds)
        return {
            "ok": True,
            "result": result,
            "pipeline": transcriber.speaker_pipeline_status(),
        }

    @app.post("/v1/transcripts/postprocess", response_model=TranscriptPostprocessResult)
    def postprocess_transcripts(req: TranscriptPostprocessRequest) -> TranscriptPostprocessResult:
        if postprocessor is None:
            raise HTTPException(
                status_code=400,
                detail="Audio archive is disabled. Enable archive_audio to post-process WAV chunks.",
            )
        try:
            return postprocessor.process_recent(req)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/v1/devices/mic")
    def list_mic_devices() -> list[dict[str, str | int]]:
        try:
            import sounddevice as sd
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="sounddevice is unavailable.") from exc
        devices = []
        for idx, dev in enumerate(sd.query_devices()):  # type: ignore[attr-defined]
            max_input = int(dev.get("max_input_channels", 0))
            if max_input > 0:
                devices.append({"index": idx, "name": str(dev.get("name", f"device-{idx}"))})
        return devices

    @app.get("/v1/devices/mic/levels")
    def probe_mic_devices(duration_ms: int = 450) -> list[dict[str, object]]:
        try:
            import sounddevice as sd
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="sounddevice is unavailable.") from exc

        duration_ms = max(150, min(duration_ms, 1500))
        results: list[dict[str, object]] = []

        for idx, dev in enumerate(sd.query_devices()):  # type: ignore[attr-defined]
            max_input = int(dev.get("max_input_channels", 0))
            if max_input <= 0:
                continue

            name = str(dev.get("name", f"device-{idx}"))
            results.append(
                _probe_input_level(
                    sd,
                    device_index=idx,
                    device_name=name,
                    max_input_channels=max_input,
                    sample_rate=settings.sample_rate,
                    channels=settings.channels,
                    duration_ms=duration_ms,
                )
            )

        results.sort(key=lambda item: float(item.get("level_dbfs", -96.0)), reverse=True)
        return results

    @app.get("/v1/devices/mic/level/{device_index}")
    def probe_mic_device(device_index: int, duration_ms: int = 450) -> dict[str, object]:
        try:
            import sounddevice as sd
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="sounddevice is unavailable.") from exc

        duration_ms = max(150, min(duration_ms, 1500))
        devices = sd.query_devices()  # type: ignore[attr-defined]
        if device_index < 0 or device_index >= len(devices):
            raise HTTPException(status_code=404, detail=f"Unknown device index={device_index}")
        dev = devices[device_index]
        max_input = int(dev.get("max_input_channels", 0))
        if max_input <= 0:
            raise HTTPException(status_code=400, detail=f"Device index={device_index} is not an input device")
        name = str(dev.get("name", f"device-{device_index}"))
        return _probe_input_level(
            sd,
            device_index=device_index,
            device_name=name,
            max_input_channels=max_input,
            sample_rate=settings.sample_rate,
            channels=settings.channels,
            duration_ms=duration_ms,
        )

    @app.get("/v1/tts/status")
    def tts_status() -> dict[str, object]:
        return tts_client.status()

    @app.get("/v1/tts/voices")
    def tts_voices() -> dict[str, object]:
        return tts_client.voices()

    @app.post("/v1/tts/synthesize/stream")
    def stream_tts(req: TTSSynthesizeRequest) -> StreamingResponse:
        stream_iter = tts_client.stream_synthesize(
            text=req.text,
            voice=req.tts_voice,
            language=req.tts_language,
            instruct=req.tts_instruct,
        )
        return StreamingResponse(
            stream_iter,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/v1/sources/start", response_model=SourceStatus)
    def start_source(req: SourceStartRequest) -> SourceStatus:
        try:
            if req.source_type == "mic":
                runtime = source_manager.start_mic(source_id=req.source_id, device=req.device)
            elif req.source_type == "ffmpeg":
                if not req.ffmpeg_input:
                    raise HTTPException(status_code=400, detail="ffmpeg_input is required.")
                runtime = source_manager.start_ffmpeg(
                    source_id=req.source_id,
                    ffmpeg_input=req.ffmpeg_input,
                    ffmpeg_input_format=req.ffmpeg_input_format,
                    ffmpeg_extra_args=req.ffmpeg_extra_args,
                )
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported source_type={req.source_type}")
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        initial_speaker = settings.mic_speaker_name if runtime.source_id == settings.mic_source_id else None
        try:
            store.open_session(
                session_id=runtime.session_id,
                source_id=runtime.source_id,
                source_type=runtime.source_type,
                started_at=runtime.started_at,
                speaker=initial_speaker,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to persist transcript session metadata for source=%s session=%s",
                runtime.source_id,
                runtime.session_id,
            )

        return SourceStatus(
            source_id=runtime.source_id,
            source_type=runtime.source_type,
            running=runtime.runner.is_running(),
            started_at=runtime.started_at,
        )

    @app.post("/v1/sources/stop/{source_id}")
    def stop_source(source_id: str) -> dict[str, bool]:
        runtime = source_manager.get(source_id)
        if runtime is None:
            raise HTTPException(status_code=404, detail=f"Unknown source_id={source_id}")
        stopped = source_manager.stop(source_id)
        if not stopped:
            raise HTTPException(status_code=404, detail=f"Unknown source_id={source_id}")
        try:
            store.close_session(runtime.session_id, datetime.now(tz=UTC))
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to close transcript session metadata for source=%s session=%s",
                runtime.source_id,
                runtime.session_id,
            )
        return {"stopped": True}

    @app.post("/v1/ingest/transcript", response_model=TranscriptItem)
    def ingest_transcript(req: TranscriptIngestRequest) -> TranscriptItem:
        speaker = req.speaker
        if speaker is None and req.source_id == settings.mic_source_id:
            speaker = settings.mic_speaker_name
        source_type = req.source_type
        if source_type is None and req.source_id == settings.mic_source_id:
            source_type = "mic"
        record = store.add(
            source_id=req.source_id,
            started_at=req.started_at,
            ended_at=req.ended_at,
            text=req.text,
            speaker=speaker,
            session_id=req.session_id,
            source_type=source_type,
        )
        return item_from_record(record)

    @app.post("/v1/ingest/pcm")
    def ingest_pcm(req: PCMIngestRequest) -> dict[str, bool]:
        try:
            pcm = base64.b64decode(req.pcm_base64)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Invalid base64 payload.") from exc
        seg = AudioSegment(
            source_id=req.source_id,
            session_id=req.session_id,
            started_at=req.started_at.astimezone(UTC),
            ended_at=req.ended_at.astimezone(UTC),
            pcm_s16le=pcm,
            sample_rate=req.sample_rate or settings.sample_rate,
        )
        try:
            level_tracker.ingest(seg)
            transcriber.enqueue(seg)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"queued": True}

    @app.get("/v1/transcripts/recent", response_model=list[TranscriptItem])
    def recent_transcripts(
        source_id: str | None = None,
        since_seconds: int | None = 3600,
        limit: int = 20,
        compact: bool = True,
        sessionized: bool = True,
    ) -> list[TranscriptItem]:
        if sessionized:
            sessions = store.recent_sessions(limit=limit, source_id=source_id, since_seconds=since_seconds)
            if sessions:
                return [item_from_session(session) for session in sessions]
        if compact:
            records = store.recent_compact(limit=limit, source_id=source_id, since_seconds=since_seconds)
        else:
            records = store.recent(limit=limit, source_id=source_id, since_seconds=since_seconds)
        return [item_from_record(record) for record in records]

    @app.get("/v1/transcripts/sources", response_model=list[str])
    def transcript_sources(since_seconds: int | None = None) -> list[str]:
        return store.list_sources(since_seconds=since_seconds)

    @app.get("/v1/transcripts/page", response_model=TranscriptPage)
    def transcript_page(
        source_id: str | None = None,
        since_seconds: int | None = None,
        before_id: int | None = None,
        limit: int = 200,
        compact: bool = False,
        sessionized: bool = True,
    ) -> TranscriptPage:
        if sessionized:
            sessions, next_before_id, has_more = store.page_sessions(
                limit=limit,
                before_id=before_id,
                source_id=source_id,
                since_seconds=since_seconds,
            )
            if sessions:
                return TranscriptPage(
                    items=[item_from_session(session) for session in sessions],
                    next_before_id=next_before_id,
                    has_more=has_more,
                )
        items, next_before_id, has_more = store.page(
            limit=limit,
            before_id=before_id,
            source_id=source_id,
            since_seconds=since_seconds,
            compact=compact,
        )
        return TranscriptPage(
            items=[item_from_record(item) for item in items],
            next_before_id=next_before_id,
            has_more=has_more,
        )

    @app.post("/v1/query", response_model=QueryAnswer)
    def answer_query(req: QueryRequest) -> QueryAnswer:
        limit = req.limit or settings.max_query_results
        if _is_summary_question(req.question):
            evidence_limit = min(80, max(limit * 3, 24))
            evidence = store.recent_compact(
                limit=evidence_limit,
                source_id=req.source_id,
                since_seconds=req.since_seconds,
            )
        else:
            evidence = store.search(
                query_text=req.question,
                limit=limit,
                source_id=req.source_id,
                since_seconds=req.since_seconds,
            )
        result = qa_engine.answer(
            req.question,
            evidence=evidence,
            provider=req.provider,
            ollama_model=req.ollama_model,
        )
        tts_audio_base64 = None
        tts_mime_type = None
        tts_provider = None
        tts_error = None
        if req.speak:
            tts_result = tts_client.synthesize(
                text=result.answer,
                voice=req.tts_voice,
                language=req.tts_language,
                instruct=req.tts_instruct,
            )
            tts_audio_base64 = tts_result.audio_base64
            tts_mime_type = tts_result.mime_type
            tts_provider = tts_result.provider
            tts_error = tts_result.error

        return QueryAnswer(
            answer=result.answer,
            provider=result.provider,
            evidence=[item_from_record(item) for item in evidence],
            audio_base64=tts_audio_base64,
            audio_mime_type=tts_mime_type,
            tts_provider=tts_provider,
            tts_error=tts_error,
        )

    return app
