from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import logging
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from threading import Event, Lock, Thread
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx
import numpy as np

from audio_assist.archive import AudioArchiver
from audio_assist.calendar_matcher import CalendarMatcher, CalendarSessionMatch
from audio_assist.config import Settings
from audio_assist.diarization import SpeakerDiarizationClient
from audio_assist.levels import AudioLevelTracker
from audio_assist.postprocess import TranscriptPostProcessor
from audio_assist.qa import QAEngine, QAResult
from audio_assist.meeting_detector import MeetingDetector
from audio_assist.noise_suppression import NoiseSuppressor
from audio_assist.orchestrator import PreflightResult, SessionOrchestrator
from audio_assist.session_context import set_session_id
from audio_assist.schemas import (
    EventTranscriptGroup,
    EventsPage,
    PCMIngestRequest,
    QueryAnswer,
    QueryRequest,
    GoogleCalendarSyncRequest,
    SourceLanguageHintRequest,
    SourceAudioLevel,
    SourceStartRequest,
    SourceStatus,
    TTSSynthesizeRequest,
    TranscriptCalendarMatch,
    TranscriptExportRequest,
    TranscriptExportResult,
    TranscriptPage,
    TranscriptIngestRequest,
    TranscriptItem,
    TranscriptPostprocessRequest,
    TranscriptPostprocessResult,
    TranscriptTitleRequest,
    TranscriptTitleResult,
    TranscriptTranslateItem,
    TranscriptTranslateRequest,
    TranscriptTranslateResult,
)
from audio_assist.sources import SourceManager, SourceRuntime
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

NON_ENGLISH_CHAR_RE = re.compile(r"[áéíóúñü¿¡àèìòùâêîôûãõç]")
SPANISH_TOKEN_RE = re.compile(r"[a-záéíóúñü]+", re.IGNORECASE)
SPANISH_COMMON_WORDS = {
    "de",
    "la",
    "el",
    "y",
    "que",
    "los",
    "las",
    "por",
    "para",
    "con",
    "como",
    "está",
    "esta",
    "ella",
    "mujer",
    "barrio",
    "barrios",
    "caserio",
    "caserios",
    "puerto",
    "rico",
    "quiero",
    "sabe",
}

COMMON_SYSTEM_AUDIO_KEYWORDS = (
    "blackhole",
    "loopback",
    "soundflower",
    "vb-audio",
    "virtual",
    "cable",
    "stereo mix",
    "aggregate",
    "multi-output",
    "zoomaudio",
    "teams",
    "krisp",
    "cluely",
)


def _system_audio_fallback_priority(name: str) -> int:
    lowered = str(name or "").lower()
    if "cluely" in lowered:
        return 110
    if "krisp" in lowered:
        return 105
    if "loopback" in lowered:
        return 102
    if "blackhole" in lowered:
        return 100
    if "soundflower" in lowered or "vb-audio" in lowered or "cable" in lowered:
        return 98
    if "stereo mix" in lowered or "aggregate" in lowered or "multi-output" in lowered:
        return 92
    if "virtual" in lowered:
        return 88
    if "zoomaudio" in lowered:
        return 35
    if "teams" in lowered:
        return 32
    return 10


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


def _looks_like_spanish_or_non_english(text: str) -> bool:
    normalized = text.strip().lower()
    if not normalized:
        return False
    if bool(NON_ENGLISH_CHAR_RE.search(normalized)):
        return True
    tokens = SPANISH_TOKEN_RE.findall(normalized)
    if len(tokens) < 2:
        return False
    score = sum(1 for token in tokens if token in SPANISH_COMMON_WORDS)
    return score >= 2


def _select_translation_evidence(candidates: list[TranscriptRecord], limit: int) -> list[TranscriptRecord]:
    if not candidates:
        return []
    if limit <= 0:
        limit = 1
    preferred = [record for record in candidates if _looks_like_spanish_or_non_english(record.text)]
    if preferred:
        # For translation prompts, feed mostly non-English chunks to avoid polluting context.
        return preferred[:limit]
    selected: list[TranscriptRecord] = []
    seen: set[int] = set()

    for record in candidates:
        if record.id in seen:
            continue
        selected.append(record)
        seen.add(record.id)
        if len(selected) >= limit:
            return selected
    return selected


def _parse_iso_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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


def _probe_input_level_with_timeout(
    sd: object,
    *,
    device_index: int,
    device_name: str,
    max_input_channels: int,
    sample_rate: int,
    channels: int,
    duration_ms: int,
    timeout_seconds: float,
) -> dict[str, object]:
    result: dict[str, object] | None = None

    def run_probe() -> None:
        nonlocal result
        result = _probe_input_level(
            sd,
            device_index=device_index,
            device_name=device_name,
            max_input_channels=max_input_channels,
            sample_rate=sample_rate,
            channels=channels,
            duration_ms=duration_ms,
        )

    thread = Thread(target=run_probe, daemon=True)
    thread.start()
    thread.join(timeout=max(0.15, float(timeout_seconds)))
    if thread.is_alive():
        return {
            "index": device_index,
            "name": device_name,
            "max_input_channels": max_input_channels,
            "level_dbfs": -96.0,
            "peak_dbfs": -96.0,
            "silent": True,
            "error": f"probe timed out after {timeout_seconds:.2f}s",
        }
    if result is None:
        return {
            "index": device_index,
            "name": device_name,
            "max_input_channels": max_input_channels,
            "level_dbfs": -96.0,
            "peak_dbfs": -96.0,
            "silent": True,
            "error": "probe failed without result",
        }
    return result


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
        beam_size=settings.whisper_beam_size,
        best_of=settings.whisper_best_of,
        vad_filter=settings.whisper_vad_filter,
        system_audio_vad_filter=settings.whisper_system_audio_vad_filter,
        no_speech_threshold=settings.whisper_no_speech_threshold,
        system_audio_no_speech_threshold=settings.whisper_system_audio_no_speech_threshold,
        min_signal_dbfs=settings.whisper_min_signal_dbfs,
        system_audio_min_signal_dbfs=settings.whisper_system_audio_min_signal_dbfs,
        fallback_on_empty=settings.whisper_fallback_on_empty,
        fallback_beam_size=settings.whisper_fallback_beam_size,
        fallback_best_of=settings.whisper_fallback_best_of,
        fallback_vad_filter=settings.whisper_fallback_vad_filter,
        fallback_no_speech_threshold=settings.whisper_fallback_no_speech_threshold,
        fallback_min_signal_dbfs=settings.whisper_fallback_min_signal_dbfs,
        system_audio_fallback_min_signal_dbfs=settings.whisper_system_audio_fallback_min_signal_dbfs,
        queue_size=settings.transcription_queue_size,
        archive_on_drop=settings.transcription_drop_archive_fallback,
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
    calendar_matcher = CalendarMatcher(
        enabled=settings.calendar_match_enabled,
        base_url=settings.google_sync_service_url,
        timeout_seconds=settings.calendar_match_timeout_seconds,
        padding_minutes=settings.calendar_match_padding_minutes,
        min_overlap_seconds=settings.calendar_match_min_overlap_seconds,
        max_gap_seconds=settings.calendar_match_max_gap_seconds,
    )
    google_sync_process: subprocess.Popen[bytes] | None = None

    def google_sync_base_url() -> str:
        return settings.google_sync_service_url.rstrip("/")

    def google_sync_health_reachable(timeout_seconds: float = 1.0) -> bool:
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.get(f"{google_sync_base_url()}/health")
            return response.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def discover_google_sync_client_secret_path() -> Path | None:
        repo_root = Path(__file__).resolve().parents[2]
        candidate_dir = repo_root / "google"
        if not candidate_dir.exists():
            return None
        try:
            candidates = sorted(
                candidate_dir.glob("client_secret_*apps.googleusercontent.com.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except Exception:  # noqa: BLE001
            return None
        return candidates[0] if candidates else None

    def maybe_start_google_sync_service() -> None:
        nonlocal google_sync_process
        if not settings.calendar_match_enabled:
            return
        if not settings.google_sync_autostart:
            return
        if google_sync_health_reachable(timeout_seconds=1.2):
            return

        command = (settings.google_sync_autostart_command or "").strip()
        cmd = shlex.split(command) if command else []
        if not cmd:
            cmd = ["google-sync-service"]

        env = os.environ.copy()
        configured_path = (
            env.get("GOOGLE_SYNC_CLIENT_SECRET_PATH")
            or (str(settings.google_sync_client_secret_path) if settings.google_sync_client_secret_path else "")
            or (str(discover_google_sync_client_secret_path() or "")).strip()
        )
        if configured_path:
            env["GOOGLE_SYNC_CLIENT_SECRET_PATH"] = configured_path

        cwd = str(settings.google_sync_autostart_cwd) if settings.google_sync_autostart_cwd else None
        logger.info("Starting google-sync-service for calendar flow: command=%s cwd=%s", cmd, cwd or "<current>")
        if configured_path:
            logger.info("Using GOOGLE_SYNC_CLIENT_SECRET_PATH=%s", configured_path)
        try:
            google_sync_process = subprocess.Popen(  # noqa: S603
                cmd,
                cwd=cwd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            fallback_cmd = [sys.executable, "-m", "google_sync_service.main"]
            logger.info("Auto-start command not found. Falling back to %s", fallback_cmd)
            try:
                google_sync_process = subprocess.Popen(
                    fallback_cmd,
                    cwd=cwd,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to start google-sync-service automatically: %s", exc)
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to start google-sync-service automatically: %s", exc)
            return

        timeout_seconds = max(2.0, float(settings.google_sync_autostart_timeout_seconds))
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if google_sync_health_reachable(timeout_seconds=1.0):
                logger.info("google-sync-service is ready.")
                return
            if google_sync_process.poll() is not None:
                logger.warning(
                    "google-sync-service exited before becoming ready (exit_code=%s).",
                    google_sync_process.returncode,
                )
                return
            time.sleep(0.3)
        logger.warning("google-sync-service did not become ready within %.1fs.", timeout_seconds)

    noise_suppressor = NoiseSuppressor(
        enabled=settings.noise_suppression_enabled,
        mic_source_prefix=settings.mic_source_id,
        prop_decrease=settings.noise_suppression_prop_decrease,
        stationary=settings.noise_suppression_stationary,
    )
    meeting_detector = MeetingDetector()

    def handle_segment(segment: AudioSegment) -> None:
        level_tracker.ingest(segment)
        if noise_suppressor.should_suppress(segment.source_id):
            suppressed_pcm = noise_suppressor.suppress(
                segment.pcm_s16le, segment.sample_rate, segment.source_id,
            )
            segment = AudioSegment(
                source_id=segment.source_id,
                session_id=segment.session_id,
                started_at=segment.started_at,
                ended_at=segment.ended_at,
                pcm_s16le=suppressed_pcm,
                sample_rate=segment.sample_rate,
            )
        transcriber.enqueue(segment)

    def schema_calendar_match(match: CalendarSessionMatch | None) -> TranscriptCalendarMatch | None:
        if match is None:
            return None
        return TranscriptCalendarMatch(
            event_id=match.event_id,
            calendar_id=match.calendar_id,
            title=match.title,
            started_at=match.started_at,
            ended_at=match.ended_at,
            html_link=match.html_link,
            hangout_link=match.hangout_link,
            match_score=match.match_score,
            overlap_seconds=match.overlap_seconds,
            distance_seconds=match.distance_seconds,
        )

    def match_map_for_sessions(sessions: list[SessionRecord]) -> dict[str, TranscriptCalendarMatch]:
        if not calendar_matcher.enabled or not sessions:
            return {}
        matches = calendar_matcher.match_sessions(sessions)
        out: dict[str, TranscriptCalendarMatch] = {}
        for session_id, match in matches.items():
            payload = schema_calendar_match(match)
            if payload is not None:
                out[session_id] = payload
        return out

    def match_map_for_records(records: list[TranscriptRecord]) -> dict[str, TranscriptCalendarMatch]:
        if not calendar_matcher.enabled or not records:
            return {}
        # Match chunk windows directly so long-running source sessions still align to
        # individual meetings on the calendar.
        pseudo_sessions = [
            SessionRecord(
                id=record.id,
                session_id=f"chunk:{record.id}",
                source_id=record.source_id,
                source_type=None,
                started_at=record.started_at,
                ended_at=record.ended_at,
                text=record.text,
                speaker=record.speaker,
            )
            for record in records
        ]
        out = match_map_for_sessions(pseudo_sessions)

        # Also compute true session matches so chunk items can inherit a stable
        # session-level event link in grouped views.
        session_ids: list[str] = []
        seen_session_ids: set[str] = set()
        for record in records:
            session_id = str(record.session_id or "").strip()
            if not session_id or session_id in seen_session_ids:
                continue
            seen_session_ids.add(session_id)
            session_ids.append(session_id)
        if session_ids:
            true_sessions = store.sessions_by_session_ids(session_ids)
            if true_sessions:
                out.update(match_map_for_sessions(true_sessions))
        return out

    def item_from_record(
        record: TranscriptRecord,
        *,
        calendar_matches: dict[str, TranscriptCalendarMatch] | None = None,
    ) -> TranscriptItem:
        chunk_key = f"chunk:{record.id}"
        match_lookup = calendar_matches or {}
        match = match_lookup.get(chunk_key)
        if match is None and record.session_id:
            match = match_lookup.get(record.session_id)
        return TranscriptItem(
            id=record.id,
            source_id=record.source_id,
            session_id=record.session_id,
            started_at=record.started_at,
            ended_at=record.ended_at,
            text=record.text,
            speaker=record.speaker,
            calendar_match=match,
        )

    def item_from_session(
        session: SessionRecord,
        *,
        calendar_matches: dict[str, TranscriptCalendarMatch] | None = None,
    ) -> TranscriptItem:
        return TranscriptItem(
            id=session.id,
            source_id=session.source_id,
            session_id=session.session_id,
            started_at=session.started_at,
            ended_at=session.ended_at,
            text=session.text,
            speaker=session.speaker,
            calendar_match=(calendar_matches or {}).get(session.session_id),
        )

    def translation_cache_key(
        record_id: int,
        text: str,
        model_name: str,
        source_language: str | None,
        target_language: str,
    ) -> str:
        normalized_source = str(source_language or "").strip().lower() or "auto"
        normalized_target = str(target_language or "english").strip().lower() or "english"
        digest = hashlib.sha1(str(text or "").encode("utf-8", errors="ignore")).hexdigest()[:16]
        return f"{record_id}:{model_name}:{normalized_source}:{normalized_target}:{digest}"

    source_manager = SourceManager(
        on_segment=handle_segment,
        sample_rate=settings.sample_rate,
        channels=settings.channels,
        segment_seconds=settings.segment_seconds,
        segment_overlap_seconds=settings.segment_overlap_seconds,
    )
    capture_watchdog_stop = Event()
    capture_watchdog_thread: Thread | None = None
    capture_retry_after_seconds: dict[str, float] = {}

    def coerce_device_identifier(value: object) -> str | int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        raw = str(value).strip()
        if not raw:
            return None
        if raw.lstrip("-").isdigit():
            try:
                return int(raw)
            except Exception:  # noqa: BLE001
                return raw
        return raw

    def discover_system_audio_device() -> int | None:
        try:
            import sounddevice as sd
        except Exception:  # noqa: BLE001
            return None
        try:
            devices = sd.query_devices()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return None

        candidates: list[dict[str, object]] = []
        for idx, raw in enumerate(devices):
            max_input_channels = int(raw.get("max_input_channels", 0))
            if max_input_channels <= 0:
                continue
            name = str(raw.get("name", "")).strip()
            lowered = name.lower()
            score = sum(1 for keyword in COMMON_SYSTEM_AUDIO_KEYWORDS if keyword in lowered)
            if score <= 0:
                continue
            candidates.append(
                {
                    "index": idx,
                    "name": name,
                    "score": score,
                    "priority": _system_audio_fallback_priority(name),
                    "max_input_channels": max_input_channels,
                }
            )
        if not candidates:
            return None

        # Prefer whichever virtual loopback path currently has real signal.
        # This avoids sticky mis-selection when multiple virtual devices exist.
        probe_duration_ms = 300
        probe_timeout_seconds = 1.2
        activity_floor_dbfs = -78.0
        active_candidates: list[dict[str, object]] = []
        for candidate in candidates:
            idx = int(candidate["index"])
            name = str(candidate["name"])
            max_input_channels = int(candidate["max_input_channels"])
            system_probe_channels = max(
                1,
                min(
                    max_input_channels,
                    max(int(settings.channels), int(settings.capture_autostart_system_audio_channels)),
                ),
            )
            probe = _probe_input_level_with_timeout(
                sd,
                device_index=idx,
                device_name=name,
                max_input_channels=max_input_channels,
                sample_rate=settings.sample_rate,
                channels=system_probe_channels,
                duration_ms=probe_duration_ms,
                timeout_seconds=probe_timeout_seconds,
            )
            level_dbfs = float(probe.get("level_dbfs") or -96.0)
            error = str(probe.get("error") or "").strip()
            if not error and level_dbfs >= activity_floor_dbfs:
                active_candidates.append(
                    {
                        **candidate,
                        "level_dbfs": level_dbfs,
                        "peak_dbfs": float(probe.get("peak_dbfs") or -96.0),
                    }
                )

        if active_candidates:
            active_candidates.sort(
                key=lambda item: (
                    float(item.get("level_dbfs") or -96.0),
                    int(item.get("score") or 0),
                    int(item.get("max_input_channels") or 0),
                ),
                reverse=True,
            )
            selected = active_candidates[0]
            logger.info(
                "Auto-selected active system-audio device index=%s name=%s level=%.1f",
                selected["index"],
                selected["name"],
                float(selected.get("level_dbfs") or -96.0),
            )
            return int(selected["index"])

        # Fallback: best semantic match (keyword score, then channels).
        candidates.sort(
            key=lambda item: (
                int(item.get("score") or 0),
                int(item.get("priority") or 0),
                int(item.get("max_input_channels") or 0),
                int(item.get("index") or 0),
            ),
            reverse=True,
        )
        selected = candidates[0]
        logger.info(
            "Auto-selected fallback system-audio device index=%s name=%s (no active loopback signal detected).",
            selected["index"],
            selected["name"],
        )
        return int(selected["index"])

    def discover_mic_device() -> int | None:
        try:
            import sounddevice as sd
        except Exception:  # noqa: BLE001
            return None
        try:
            devices = sd.query_devices()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return None

        candidates: list[tuple[int, str, int]] = []
        for idx, raw in enumerate(devices):
            max_input_channels = int(raw.get("max_input_channels", 0))
            if max_input_channels <= 0:
                continue
            name = str(raw.get("name", "")).strip()
            lowered = name.lower()
            # Prefer physical microphone paths, avoid virtual loopback/system-audio devices.
            if any(keyword in lowered for keyword in COMMON_SYSTEM_AUDIO_KEYWORDS):
                continue
            score = 0
            if "macbook" in lowered:
                score += 6
            if "microphone" in lowered:
                score += 5
            if "built-in" in lowered or "internal" in lowered:
                score += 4
            candidates.append((idx, name, score))

        if not candidates:
            return None
        candidates.sort(key=lambda row: (row[2], -row[0]), reverse=True)
        selected_idx, selected_name, _ = candidates[0]
        logger.info("Auto-selected mic device index=%s name=%s", selected_idx, selected_name)
        return int(selected_idx)

    def close_stale_session(runtime: SourceRuntime | None) -> None:
        if runtime is None:
            return
        try:
            store.close_session(runtime.session_id, datetime.now(tz=UTC))
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to close stale transcript session metadata for source=%s session=%s",
                runtime.source_id,
                runtime.session_id,
            )

    def persist_runtime_session(runtime: SourceRuntime) -> None:
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

    def ensure_capture_sources_running() -> dict[str, object]:
        if not settings.capture_autostart_enabled:
            return {
                "enabled": False,
                "started_count": 0,
                "running_count": len([item for item in source_manager.statuses() if item.runner.is_running()]),
                "errors": [],
                "sources": [],
            }

        report: list[dict[str, object]] = []
        errors: list[str] = []
        now_monotonic = time.monotonic()
        retry_cooldown_seconds = max(10.0, float(settings.capture_autostart_retry_cooldown_seconds))

        def mark_error(message: str) -> None:
            errors.append(message)
            logger.warning(message)

        def ensure_mic_source(
            source_id: str,
            device: str | int | None,
            role: str,
            channels: int | None = None,
        ) -> None:
            retry_after = float(capture_retry_after_seconds.get(source_id, 0.0))
            if retry_after > now_monotonic:
                wait_seconds = max(0.0, retry_after - now_monotonic)
                report.append(
                    {
                        "source_id": source_id,
                        "source_type": "mic",
                        "role": role,
                        "started": False,
                        "running": False,
                        "reason": "retry-cooldown",
                        "retry_in_seconds": round(wait_seconds, 1),
                    }
                )
                return
            existing = source_manager.get(source_id)
            if existing is not None and existing.runner.is_running():
                report.append(
                    {
                        "source_id": source_id,
                        "source_type": "mic",
                        "role": role,
                        "started": False,
                        "running": True,
                        "reason": "already-running",
                    }
                )
                return
            if existing is not None:
                close_stale_session(existing)
                try:
                    source_manager.stop(source_id)
                except Exception:  # noqa: BLE001
                    logger.exception("Failed stopping stale mic source=%s before restart.", source_id)
            try:
                runtime = source_manager.start_mic(
                    source_id=source_id, device=device, channels=channels, source_role=role,
                )
                persist_runtime_session(runtime)
                capture_retry_after_seconds.pop(source_id, None)
                report.append(
                    {
                        "source_id": source_id,
                        "source_type": "mic",
                        "role": role,
                        "started": True,
                        "running": runtime.runner.is_running(),
                        "device": device,
                        "channels": channels,
                    }
                )
                logger.info("Auto-started source=%s (mic, role=%s, device=%s)", source_id, role, device)
            except Exception as exc:  # noqa: BLE001
                mark_error(f"Failed to auto-start mic source '{source_id}' ({role}): {exc}")
                capture_retry_after_seconds[source_id] = now_monotonic + retry_cooldown_seconds
                report.append(
                    {
                        "source_id": source_id,
                        "source_type": "mic",
                        "role": role,
                        "started": False,
                        "running": False,
                        "error": str(exc),
                    }
                )

        def ensure_ffmpeg_source(
            source_id: str,
            ffmpeg_input: str,
            ffmpeg_input_format: str | None,
            role: str,
        ) -> None:
            retry_after = float(capture_retry_after_seconds.get(source_id, 0.0))
            if retry_after > now_monotonic:
                wait_seconds = max(0.0, retry_after - now_monotonic)
                report.append(
                    {
                        "source_id": source_id,
                        "source_type": "ffmpeg",
                        "role": role,
                        "started": False,
                        "running": False,
                        "reason": "retry-cooldown",
                        "retry_in_seconds": round(wait_seconds, 1),
                    }
                )
                return
            existing = source_manager.get(source_id)
            if existing is not None and existing.runner.is_running():
                report.append(
                    {
                        "source_id": source_id,
                        "source_type": "ffmpeg",
                        "role": role,
                        "started": False,
                        "running": True,
                        "reason": "already-running",
                    }
                )
                return
            if existing is not None:
                close_stale_session(existing)
                try:
                    source_manager.stop(source_id)
                except Exception:  # noqa: BLE001
                    logger.exception("Failed stopping stale ffmpeg source=%s before restart.", source_id)
            try:
                runtime = source_manager.start_ffmpeg(
                    source_id=source_id,
                    ffmpeg_input=ffmpeg_input,
                    ffmpeg_input_format=ffmpeg_input_format,
                )
                persist_runtime_session(runtime)
                capture_retry_after_seconds.pop(source_id, None)
                report.append(
                    {
                        "source_id": source_id,
                        "source_type": "ffmpeg",
                        "role": role,
                        "started": True,
                        "running": runtime.runner.is_running(),
                        "ffmpeg_input": ffmpeg_input,
                        "ffmpeg_input_format": ffmpeg_input_format,
                    }
                )
                logger.info(
                    "Auto-started source=%s (ffmpeg, role=%s, input=%s)",
                    source_id,
                    role,
                    ffmpeg_input,
                )
            except Exception as exc:  # noqa: BLE001
                mark_error(f"Failed to auto-start ffmpeg source '{source_id}' ({role}): {exc}")
                capture_retry_after_seconds[source_id] = now_monotonic + retry_cooldown_seconds
                report.append(
                    {
                        "source_id": source_id,
                        "source_type": "ffmpeg",
                        "role": role,
                        "started": False,
                        "running": False,
                        "error": str(exc),
                    }
                )

        if settings.capture_autostart_mic_enabled:
            mic_source_id = str(settings.capture_autostart_mic_source_id or settings.mic_source_id).strip() or "desk-mic"
            explicit_mic_device = coerce_device_identifier(settings.capture_autostart_mic_device)
            mic_device = explicit_mic_device if explicit_mic_device is not None else discover_mic_device()
            ensure_mic_source(mic_source_id, mic_device, role="desk-mic")

        if settings.capture_autostart_system_audio_enabled:
            system_source_id = str(settings.capture_autostart_system_audio_source_id or "system-audio").strip() or "system-audio"
            if settings.capture_autostart_mic_enabled and system_source_id == str(settings.capture_autostart_mic_source_id).strip():
                mark_error("System-audio auto-start skipped because source_id collides with mic source_id.")
                report.append(
                    {
                        "source_id": system_source_id,
                        "source_type": "mic",
                        "role": "system-audio",
                        "started": False,
                        "running": False,
                        "error": "source-id-collision",
                    }
                )
            else:
                explicit_device = coerce_device_identifier(settings.capture_autostart_system_audio_device)
                discovered_device = discover_system_audio_device() if explicit_device is None else None
                selected_device = explicit_device if explicit_device is not None else discovered_device
                system_channels = max(1, min(int(settings.capture_autostart_system_audio_channels), 8))
                if selected_device is not None:
                    ensure_mic_source(
                        system_source_id,
                        selected_device,
                        role="system-audio",
                        channels=system_channels,
                    )
                else:
                    ffmpeg_input = str(settings.capture_autostart_system_audio_ffmpeg_input or "").strip()
                    ffmpeg_input_format = (
                        str(settings.capture_autostart_system_audio_ffmpeg_input_format or "").strip() or None
                    )
                    if ffmpeg_input:
                        ensure_ffmpeg_source(
                            system_source_id,
                            ffmpeg_input=ffmpeg_input,
                            ffmpeg_input_format=ffmpeg_input_format,
                            role="system-audio",
                        )
                    else:
                        report.append(
                            {
                                "source_id": system_source_id,
                                "source_type": "mic",
                                "role": "system-audio",
                                "started": False,
                                "running": False,
                                "reason": "no-system-audio-device-found",
                            }
                        )

        running_count = len([item for item in source_manager.statuses() if item.runner.is_running()])
        started_count = len([item for item in report if bool(item.get("started"))])
        return {
            "enabled": True,
            "started_count": started_count,
            "running_count": running_count,
            "errors": errors,
            "sources": report,
        }

    def capture_watchdog_loop() -> None:
        interval_seconds = max(5.0, float(settings.capture_watchdog_interval_seconds))
        while not capture_watchdog_stop.wait(interval_seconds):
            try:
                ensure_capture_sources_running()
            except Exception:  # noqa: BLE001
                logger.exception("Capture watchdog ensure failed.")
    qa_engine = QAEngine(
        provider=settings.qa_provider,
        ollama_url=settings.ollama_url,
        ollama_model=settings.ollama_model,
        timeout_seconds=settings.ollama_timeout_seconds,
    )
    translation_cache_lock = Lock()
    translation_cache: dict[str, str] = {}
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
            default_cpu_threads=settings.whisper_postprocess_cpu_threads,
            default_num_workers=settings.whisper_postprocess_num_workers,
            default_parallelism=settings.whisper_postprocess_parallelism,
            default_beam_size=settings.whisper_postprocess_beam_size,
            default_best_of=settings.whisper_postprocess_best_of,
            default_vad_filter=settings.whisper_postprocess_vad_filter,
            default_no_speech_threshold=settings.whisper_postprocess_no_speech_threshold,
        )
        if archiver is not None
        else None
    )

    def _capture_preflight() -> PreflightResult:
        """Run preflight checks for the orchestrator before recording a meeting."""
        checks: dict[str, bool] = {}
        reasons: list[str] = []

        runtimes = source_manager.statuses()
        running = [r for r in runtimes if r.runner.is_running()]
        checks["sources_running"] = len(running) > 0
        if not checks["sources_running"]:
            reasons.append("No capture sources are running.")

        mic_running = any(r.source_id == settings.mic_source_id for r in running)
        checks["mic_running"] = mic_running
        if not mic_running:
            reasons.append(f"Mic source '{settings.mic_source_id}' is not running.")

        pipeline = transcriber.transcription_pipeline_status()
        queue_ratio = 0.0
        capacity = int(pipeline.get("queue_capacity") or 1)
        if capacity > 0:
            queue_ratio = int(pipeline.get("queue_size") or 0) / capacity
        checks["queue_healthy"] = queue_ratio < 0.8
        if not checks["queue_healthy"]:
            reasons.append(f"Transcription queue pressure is high ({queue_ratio:.0%}).")

        passed = all(checks.values())
        return PreflightResult(passed=passed, checks=checks, reasons=reasons)

    def _on_meeting_session_start(session) -> None:  # noqa: ANN001
        """Called by orchestrator when a meeting session begins."""
        set_session_id(session.session_id)
        logger.info(
            "Meeting session started: id=%s provider=%s title=%s",
            session.session_id, session.provider, session.title,
        )
        # Ensure capture sources are running
        ensure_capture_sources_running()

    def _on_meeting_session_end(session) -> None:  # noqa: ANN001
        """Called by orchestrator when a meeting session ends."""
        logger.info(
            "Meeting session ended: id=%s provider=%s title=%s duration=%.0fs",
            session.session_id, session.provider, session.title,
            (session.ended_at - session.started_at).total_seconds() if session.ended_at else 0,
        )
        set_session_id(None)

    session_orchestrator = SessionOrchestrator(
        meeting_detector=meeting_detector,
        poll_interval_seconds=settings.orchestrator_poll_interval_seconds,
        preparing_timeout_seconds=settings.orchestrator_preparing_timeout_seconds,
        cooldown_seconds=settings.orchestrator_cooldown_seconds,
        min_active_polls_to_record=settings.orchestrator_min_active_polls,
        on_session_start=_on_meeting_session_start,
        on_session_end=_on_meeting_session_end,
        preflight_fn=_capture_preflight,
    )

    @app.on_event("startup")
    def startup() -> None:
        nonlocal capture_watchdog_thread
        logger.info("Starting transcription worker...")
        transcriber.start()
        maybe_start_google_sync_service()
        capture_bootstrap = ensure_capture_sources_running()
        logger.info(
            "Capture ensure complete: started=%s running=%s errors=%s",
            capture_bootstrap.get("started_count"),
            capture_bootstrap.get("running_count"),
            len(capture_bootstrap.get("errors") or []),
        )
        if settings.capture_watchdog_enabled:
            capture_watchdog_stop.clear()
            capture_watchdog_thread = Thread(target=capture_watchdog_loop, daemon=True)
            capture_watchdog_thread.start()
            logger.info(
                "Capture watchdog started (interval=%.1fs).",
                max(5.0, float(settings.capture_watchdog_interval_seconds)),
            )
        session_orchestrator.start()
        logger.info("audio-assist started.")

    @app.on_event("shutdown")
    def shutdown() -> None:
        session_orchestrator.stop()
        capture_watchdog_stop.set()
        if capture_watchdog_thread is not None and capture_watchdog_thread.is_alive():
            capture_watchdog_thread.join(timeout=2.5)
        source_manager.stop_all()
        transcriber.stop()
        if google_sync_process is not None and google_sync_process.poll() is None:
            logger.info("Stopping auto-started google-sync-service (pid=%s)...", google_sync_process.pid)
            google_sync_process.terminate()
            try:
                google_sync_process.wait(timeout=3.0)
            except Exception:  # noqa: BLE001
                google_sync_process.kill()
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

    @app.get("/events")
    def events_page_ui() -> FileResponse:
        return FileResponse(static_dir / "events.html")

    @app.get("/v1/google-sync/connect")
    def google_sync_connect() -> RedirectResponse:
        return RedirectResponse(f"{google_sync_base_url()}/connect")

    @app.get("/v1/google-sync/status")
    def google_sync_status() -> dict[str, object]:
        base_url = google_sync_base_url()
        payload: dict[str, object] = {
            "enabled": bool(settings.calendar_match_enabled),
            "service_url": base_url,
            "connect_url": "/v1/google-sync/connect",
            "reachable": False,
            "configured": None,
            "connected": None,
            "needs_user_action": None,
            "scopes": [],
            "calendar_cached": False,
            "calendar_synced_at": None,
            "calendar_event_count": None,
            "error": None,
        }
        timeout_seconds = max(2.0, float(settings.calendar_match_timeout_seconds))
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                auth_resp = client.get(f"{base_url}/v1/auth/status")
                auth_resp.raise_for_status()
                auth_payload = auth_resp.json() if auth_resp.content else {}
                payload["reachable"] = True
                payload["configured"] = bool(auth_payload.get("configured"))
                payload["connected"] = bool(auth_payload.get("connected"))
                payload["needs_user_action"] = bool(auth_payload.get("needs_user_action"))
                payload["scopes"] = auth_payload.get("scopes") or []

                latest_resp = client.get(f"{base_url}/v1/calendar/latest")
                if latest_resp.status_code == 200:
                    latest_payload = latest_resp.json() if latest_resp.content else {}
                    payload["calendar_cached"] = True
                    payload["calendar_synced_at"] = latest_payload.get("synced_at")
                    payload["calendar_event_count"] = latest_payload.get("count")
                elif latest_resp.status_code == 404:
                    payload["calendar_cached"] = False
                else:
                    latest_resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            payload["error"] = str(exc)
        return payload

    @app.post("/v1/google-sync/calendar/sync")
    def google_sync_calendar_sync(req: GoogleCalendarSyncRequest) -> dict[str, object]:
        base_url = google_sync_base_url()
        now = datetime.now(tz=UTC)
        body = {
            "calendar_id": req.calendar_id,
            "time_min": (now - timedelta(hours=req.lookback_hours)).isoformat(),
            "time_max": (now + timedelta(hours=req.lookahead_hours)).isoformat(),
            "max_results": req.max_results,
            "query": req.query,
        }
        timeout_seconds = max(4.0, float(settings.calendar_match_timeout_seconds) * 2.0)
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.post(f"{base_url}/v1/calendar/sync", json=body)
            if response.status_code == 401:
                raise HTTPException(status_code=401, detail="Google account is not connected. Please connect first.")
            if response.status_code >= 400:
                detail = f"Calendar sync failed ({response.status_code})."
                try:
                    payload = response.json()
                    if payload and payload.get("detail"):
                        detail = str(payload.get("detail"))
                except Exception:  # noqa: BLE001
                    pass
                raise HTTPException(status_code=502, detail=detail)
            data = response.json() if response.content else {}
            return {
                "ok": True,
                "calendar_id": data.get("calendar_id"),
                "count": data.get("count"),
                "synced_at": data.get("synced_at"),
                "events": data.get("events") or [],
            }
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Calendar sync service unavailable: {exc}") from exc

    @app.get("/v1/google-sync/calendar/latest")
    def google_sync_calendar_latest() -> dict[str, object]:
        base_url = google_sync_base_url()
        timeout_seconds = max(3.0, float(settings.calendar_match_timeout_seconds) * 1.5)
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.get(f"{base_url}/v1/calendar/latest")
            if response.status_code == 404:
                return {
                    "ok": True,
                    "calendar_id": None,
                    "count": 0,
                    "synced_at": None,
                    "events": [],
                }
            if response.status_code == 401:
                raise HTTPException(status_code=401, detail="Google account is not connected. Please connect first.")
            if response.status_code >= 400:
                detail = f"Calendar latest fetch failed ({response.status_code})."
                try:
                    payload = response.json()
                    if payload and payload.get("detail"):
                        detail = str(payload.get("detail"))
                except Exception:  # noqa: BLE001
                    pass
                raise HTTPException(status_code=502, detail=detail)
            data = response.json() if response.content else {}
            return {
                "ok": True,
                "calendar_id": data.get("calendar_id"),
                "count": data.get("count"),
                "synced_at": data.get("synced_at"),
                "events": data.get("events") or [],
            }
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=f"Calendar latest fetch failed: {exc}",
            ) from exc

    @app.get("/v1/events/page", response_model=EventsPage)
    def events_page_data(
        lookback_hours: int = 168,
        lookahead_hours: int = 168,
        transcript_limit: int = 6000,
        transcripts_per_event: int = 120,
        source_id: str | None = None,
    ) -> EventsPage:
        latest_payload = google_sync_calendar_latest()
        raw_events = latest_payload.get("events")
        if not isinstance(raw_events, list):
            raw_events = []

        lookback = max(1, min(int(lookback_hours), 24 * 30))
        lookahead = max(1, min(int(lookahead_hours), 24 * 30))
        safe_transcript_limit = max(1, min(int(transcript_limit), 10000))
        safe_per_event_limit = max(0, min(int(transcripts_per_event), 500))

        now = datetime.now(tz=UTC)
        window_min = now - timedelta(hours=lookback)
        window_max = now + timedelta(hours=lookahead)

        normalized_events: list[dict[str, object]] = []
        for raw in raw_events:
            if not isinstance(raw, dict):
                continue
            started_at = _parse_iso_datetime(raw.get("start_at"))
            ended_at = _parse_iso_datetime(raw.get("end_at"))
            if started_at is None or ended_at is None or ended_at <= started_at:
                continue
            if ended_at < window_min or started_at > window_max:
                continue
            event_id = str(raw.get("event_id") or "").strip()
            if not event_id:
                continue
            normalized_events.append(
                {
                    "event_id": event_id,
                    "calendar_id": str(raw.get("calendar_id") or "").strip() or "primary",
                    "title": str(raw.get("summary") or "").strip() or "(Untitled meeting)",
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "html_link": str(raw.get("html_link") or "").strip() or None,
                    "hangout_link": str(raw.get("hangout_link") or "").strip() or None,
                }
            )
        normalized_events.sort(key=lambda item: (item["started_at"], item["ended_at"]))

        overlap_records: list[TranscriptRecord] = []
        if normalized_events:
            query_start = min(item["started_at"] for item in normalized_events)
            query_end = max(item["ended_at"] for item in normalized_events)
            overlap_records = store.overlap_window(
                start_at=query_start,
                end_at=query_end,
                source_id=source_id,
                limit=safe_transcript_limit,
            )

        calendar_matches = match_map_for_records(overlap_records)
        event_groups: list[EventTranscriptGroup] = []
        for event in normalized_events:
            started_at = event["started_at"]
            ended_at = event["ended_at"]
            matches = [
                record
                for record in overlap_records
                if record.started_at < ended_at and record.ended_at > started_at
            ]
            matches.sort(key=lambda item: (item.started_at, item.id))
            selected = matches if safe_per_event_limit == 0 else matches[:safe_per_event_limit]
            event_groups.append(
                EventTranscriptGroup(
                    event_id=str(event["event_id"]),
                    calendar_id=str(event["calendar_id"]),
                    title=str(event["title"]),
                    started_at=started_at,
                    ended_at=ended_at,
                    html_link=event["html_link"],
                    hangout_link=event["hangout_link"],
                    overlap_count=len(matches),
                    transcripts=[
                        item_from_record(record, calendar_matches=calendar_matches)
                        for record in selected
                    ],
                )
            )

        return EventsPage(
            calendar_id=str(latest_payload.get("calendar_id") or "").strip() or None,
            synced_at=_parse_iso_datetime(latest_payload.get("synced_at")),
            count=len(event_groups),
            events=event_groups,
        )

    @app.get("/v1/sources", response_model=list[SourceStatus])
    def list_sources() -> list[SourceStatus]:
        statuses = []
        for runtime in source_manager.statuses():
            hint = transcriber.language_hint_for_source(runtime.source_id)
            details: dict[str, str] = {}
            if hint:
                details["language_hint"] = hint
            statuses.append(
                SourceStatus(
                    source_id=runtime.source_id,
                    source_type=runtime.source_type,
                    source_role=runtime.source_role,
                    running=runtime.runner.is_running(),
                    started_at=runtime.started_at,
                    details=details,
                )
            )
        return statuses

    @app.post("/v1/sources/ensure")
    def ensure_sources() -> dict[str, object]:
        payload = ensure_capture_sources_running()
        return payload

    @app.get("/v1/capture/readiness")
    def capture_readiness(
        recent_seconds: int = 180,
        level_stale_seconds: int = 25,
        active_level_dbfs: float = -55.0,
        ensure_running: bool = False,
    ) -> dict[str, object]:
        safe_recent_seconds = int(max(30, min(recent_seconds, 3600)))
        safe_level_stale_seconds = int(max(3, min(level_stale_seconds, 180)))
        safe_active_level_dbfs = float(max(-80.0, min(active_level_dbfs, -8.0)))

        auto_ensure_payload: dict[str, object] | None = None
        if ensure_running and settings.capture_autostart_enabled:
            auto_ensure_payload = ensure_capture_sources_running()
        now = datetime.now(tz=UTC)

        runtimes = source_manager.statuses()
        running_ids = [runtime.source_id for runtime in runtimes if runtime.runner.is_running()]
        runtime_by_source = {runtime.source_id: runtime for runtime in runtimes}
        level_map = {item.source_id: item for item in level_tracker.snapshot()}
        transcriber_pipeline = transcriber.transcription_pipeline_status()

        mic_source_id = str(settings.capture_autostart_mic_source_id or settings.mic_source_id).strip() or "desk-mic"
        system_source_id = (
            str(settings.capture_autostart_system_audio_source_id or "system-audio").strip() or "system-audio"
        )
        system_ffmpeg_input = str(settings.capture_autostart_system_audio_ffmpeg_input or "").strip()
        system_explicit_config = (
            settings.capture_autostart_system_audio_device is not None or bool(system_ffmpeg_input)
        )
        system_known = system_source_id in {runtime.source_id for runtime in runtimes}
        required_mic = bool(settings.capture_autostart_enabled and settings.capture_autostart_mic_enabled)
        required_system_audio = bool(
            settings.capture_autostart_enabled
            and settings.capture_autostart_system_audio_enabled
            and (system_explicit_config or system_known)
        )

        total_recent_count, total_recent_latest = store.recent_activity(since_seconds=safe_recent_seconds)
        mic_recent_count, mic_recent_latest = store.recent_activity(
            since_seconds=safe_recent_seconds,
            source_id=mic_source_id,
        )
        system_recent_count, system_recent_latest = store.recent_activity(
            since_seconds=safe_recent_seconds,
            source_id=system_source_id,
        )

        issues: list[dict[str, str]] = []
        recommendations: list[str] = []

        def add_issue(
            *,
            code: str,
            severity: str,
            message: str,
            source_id: str | None = None,
            recommendation: str | None = None,
        ) -> None:
            payload = {
                "code": code,
                "severity": severity,
                "message": message,
            }
            if source_id:
                payload["source_id"] = source_id
            issues.append(payload)
            if recommendation and recommendation not in recommendations:
                recommendations.append(recommendation)

        if not running_ids:
            add_issue(
                code="no_running_sources",
                severity="critical",
                message="No capture sources are running.",
                recommendation="Start capture sources or click 'Ensure Capture Running' on the Events page.",
            )

        if required_mic and mic_source_id not in running_ids:
            add_issue(
                code="required_mic_not_running",
                severity="critical",
                source_id=mic_source_id,
                message=f"Required mic source '{mic_source_id}' is not running.",
                recommendation="Restart the mic source immediately before critical sessions.",
            )

        if required_system_audio and system_source_id not in running_ids:
            add_issue(
                code="required_system_audio_not_running",
                severity="warning",
                source_id=system_source_id,
                message=f"Required system-audio source '{system_source_id}' is not running.",
                recommendation="Start system-audio capture if meeting audio output is important.",
            )

        for source_id in running_ids:
            snapshot = level_map.get(source_id)
            if snapshot is None:
                runtime = runtime_by_source.get(source_id)
                runtime_age_seconds = (
                    max(0.0, (now - runtime.started_at).total_seconds()) if runtime is not None else 0.0
                )
                if runtime_age_seconds > safe_level_stale_seconds:
                    add_issue(
                        code="audio_level_missing",
                        severity="warning",
                        source_id=source_id,
                        message=f"Source '{source_id}' has no audio level telemetry yet.",
                        recommendation="Speak into the mic/play system audio to verify input path.",
                    )
                continue
            if snapshot.age_seconds > safe_level_stale_seconds:
                add_issue(
                    code="audio_level_stale",
                    severity="warning",
                    source_id=source_id,
                    message=(
                        f"Source '{source_id}' level telemetry is stale "
                        f"({snapshot.age_seconds:.0f}s old)."
                    ),
                    recommendation="Restart stale sources to avoid silent failures.",
                )

        mic_level = level_map.get(mic_source_id)
        mic_running = mic_source_id in running_ids
        mic_signal_active = bool(
            mic_level is not None
            and mic_level.age_seconds <= safe_level_stale_seconds
            and not mic_level.silent
            and mic_level.level_dbfs >= safe_active_level_dbfs
        )
        if mic_running and mic_signal_active and mic_recent_count == 0:
            add_issue(
                code="mic_active_without_transcript",
                severity="critical",
                source_id=mic_source_id,
                message=(
                    f"Mic '{mic_source_id}' has active signal but no transcript text in the last "
                    f"{safe_recent_seconds}s."
                ),
                recommendation="Check mic routing and ASR thresholds; active mic should produce transcript rows.",
            )

        system_level = level_map.get(system_source_id)
        system_running = system_source_id in running_ids
        system_signal_active = bool(
            system_level is not None
            and system_level.age_seconds <= safe_level_stale_seconds
            and not system_level.silent
            and system_level.level_dbfs >= safe_active_level_dbfs
        )
        if required_system_audio and system_running and mic_recent_count > 0 and system_recent_count == 0:
            severity = "critical" if system_signal_active else "warning"
            add_issue(
                code="system_audio_missing_during_active_capture",
                severity=severity,
                source_id=system_source_id,
                message=(
                    f"System-audio source '{system_source_id}' has no transcript rows in the last "
                    f"{safe_recent_seconds}s while mic activity is present."
                ),
                recommendation=(
                    "Verify macOS output routing into your virtual loopback (for example BlackHole/Cluely) "
                    "or switch the system-audio source device."
                ),
            )

        queue_size = int(transcriber_pipeline.get("queue_size") or 0)
        queue_capacity = int(transcriber_pipeline.get("queue_capacity") or 0)
        queue_ratio = (queue_size / queue_capacity) if queue_capacity > 0 else 0.0
        if queue_ratio >= 0.9:
            add_issue(
                code="transcriber_queue_critical",
                severity="critical",
                message=f"Transcription queue is saturated ({queue_size}/{queue_capacity}).",
                recommendation="Reduce input pressure or restart service to prevent dropped segments.",
            )
        elif queue_ratio >= 0.7:
            add_issue(
                code="transcriber_queue_high",
                severity="warning",
                message=f"Transcription queue is high ({queue_size}/{queue_capacity}).",
                recommendation="Monitor queue pressure to avoid drops.",
            )

        critical_issues = [issue for issue in issues if issue.get("severity") == "critical"]
        warning_issues = [issue for issue in issues if issue.get("severity") == "warning"]
        if critical_issues:
            status = "down"
            summary = f"{len(critical_issues)} critical issue(s) blocking reliable capture."
        elif warning_issues:
            status = "degraded"
            summary = f"{len(warning_issues)} warning(s); capture is running but needs attention."
        else:
            status = "ready"
            summary = f"Capture healthy. {len(running_ids)} source(s) running with recent transcript activity."

        levels_payload: list[dict[str, object]] = []
        for source_id in sorted(set(running_ids) | {mic_source_id, system_source_id}):
            snapshot = level_map.get(source_id)
            if snapshot is None:
                levels_payload.append(
                    {
                        "source_id": source_id,
                        "level_dbfs": None,
                        "peak_dbfs": None,
                        "age_seconds": None,
                        "silent": None,
                        "clipped": None,
                        "updated_at": None,
                    }
                )
                continue
            levels_payload.append(
                {
                    "source_id": source_id,
                    "level_dbfs": snapshot.level_dbfs,
                    "peak_dbfs": snapshot.peak_dbfs,
                    "age_seconds": snapshot.age_seconds,
                    "silent": snapshot.silent,
                    "clipped": snapshot.clipped,
                    "updated_at": snapshot.updated_at,
                }
            )

        return {
            "status": status,
            "summary": summary,
            "checked_at": now,
            "issues": issues,
            "recommendations": recommendations,
            "signals": {
                "recent_seconds": safe_recent_seconds,
                "level_stale_seconds": safe_level_stale_seconds,
                "active_level_dbfs": safe_active_level_dbfs,
                "running_source_count": len(running_ids),
                "running_sources": sorted(running_ids),
                "required": {
                    "mic": required_mic,
                    "system_audio": required_system_audio,
                    "mic_source_id": mic_source_id,
                    "system_audio_source_id": system_source_id,
                },
                "auto_ensure": auto_ensure_payload,
                "transcripts_recent_total": total_recent_count,
                "transcripts_recent_mic": mic_recent_count,
                "transcripts_recent_system_audio": system_recent_count,
                "latest_transcript_at": total_recent_latest,
                "latest_mic_transcript_at": mic_recent_latest,
                "latest_system_audio_transcript_at": system_recent_latest,
                "mic_signal_active": mic_signal_active,
                "transcriber": {
                    "queue_size": queue_size,
                    "queue_capacity": queue_capacity,
                    "queue_ratio": round(queue_ratio, 4),
                    "processed": int(transcriber_pipeline.get("processed") or 0),
                    "with_text": int(transcriber_pipeline.get("with_text") or 0),
                    "empty_text": int(transcriber_pipeline.get("empty_text") or 0),
                },
                "levels": levels_payload,
            },
        }

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

    @app.get("/v1/transcriber/status")
    def transcriber_status() -> dict[str, object]:
        return transcriber.transcription_pipeline_status()

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
            lowered = name.lower()
            probe_channels = settings.channels
            if any(keyword in lowered for keyword in COMMON_SYSTEM_AUDIO_KEYWORDS):
                probe_channels = max(settings.channels, settings.capture_autostart_system_audio_channels)
            results.append(
                _probe_input_level_with_timeout(
                    sd,
                    device_index=idx,
                    device_name=name,
                    max_input_channels=max_input,
                    sample_rate=settings.sample_rate,
                    channels=probe_channels,
                    duration_ms=duration_ms,
                    timeout_seconds=max(0.8, (duration_ms / 1000.0) + 0.6),
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
        lowered = name.lower()
        probe_channels = settings.channels
        if any(keyword in lowered for keyword in COMMON_SYSTEM_AUDIO_KEYWORDS):
            probe_channels = max(settings.channels, settings.capture_autostart_system_audio_channels)
        return _probe_input_level_with_timeout(
            sd,
            device_index=device_index,
            device_name=name,
            max_input_channels=max_input,
            sample_rate=settings.sample_rate,
            channels=probe_channels,
            duration_ms=duration_ms,
            timeout_seconds=max(0.8, (duration_ms / 1000.0) + 0.6),
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
            if req.source_id:
                existing = source_manager.get(req.source_id)
                if existing is not None and not existing.runner.is_running():
                    close_stale_session(existing)
            if req.source_type == "mic":
                runtime = source_manager.start_mic(source_id=req.source_id, device=req.device, channels=req.channels)
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

        applied_hint = transcriber.set_source_language_hint(runtime.source_id, req.language_hint)
        persist_runtime_session(runtime)

        return SourceStatus(
            source_id=runtime.source_id,
            source_type=runtime.source_type,
            source_role=runtime.source_role,
            running=runtime.runner.is_running(),
            started_at=runtime.started_at,
            details={"language_hint": applied_hint} if applied_hint else {},
        )

    @app.post("/v1/sources/language", response_model=SourceStatus)
    def update_source_language(req: SourceLanguageHintRequest) -> SourceStatus:
        runtime = source_manager.get(req.source_id)
        if runtime is None:
            raise HTTPException(status_code=404, detail=f"Unknown source_id={req.source_id}")
        applied_hint = transcriber.set_source_language_hint(runtime.source_id, req.language_hint)
        return SourceStatus(
            source_id=runtime.source_id,
            source_type=runtime.source_type,
            source_role=runtime.source_role,
            running=runtime.runner.is_running(),
            started_at=runtime.started_at,
            details={"language_hint": applied_hint} if applied_hint else {},
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
                calendar_matches = match_map_for_sessions(sessions)
                return [item_from_session(session, calendar_matches=calendar_matches) for session in sessions]
        if compact:
            records = store.recent_compact(limit=limit, source_id=source_id, since_seconds=since_seconds)
        else:
            records = store.recent(limit=limit, source_id=source_id, since_seconds=since_seconds)
        calendar_matches = match_map_for_records(records)
        return [item_from_record(record, calendar_matches=calendar_matches) for record in records]

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
                calendar_matches = match_map_for_sessions(sessions)
                return TranscriptPage(
                    items=[item_from_session(session, calendar_matches=calendar_matches) for session in sessions],
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
        calendar_matches = match_map_for_records(items)
        return TranscriptPage(
            items=[item_from_record(item, calendar_matches=calendar_matches) for item in items],
            next_before_id=next_before_id,
            has_more=has_more,
        )

    @app.post("/v1/transcripts/title", response_model=TranscriptTitleResult)
    def transcript_title(req: TranscriptTitleRequest) -> TranscriptTitleResult:
        requested_ids: list[int] = []
        seen: set[int] = set()
        for raw_id in req.transcript_ids:
            try:
                item_id = int(raw_id)
            except Exception:  # noqa: BLE001
                continue
            if item_id <= 0 or item_id in seen:
                continue
            seen.add(item_id)
            requested_ids.append(item_id)
        if not requested_ids:
            raise HTTPException(status_code=400, detail="transcript_ids is required.")

        records = store.by_ids(requested_ids)
        if not records:
            raise HTTPException(status_code=404, detail="No transcripts found for provided ids.")
        model_name = (req.model_name or settings.ollama_model).strip() or settings.ollama_model
        result = qa_engine.generate_title(records, model=model_name, max_words=req.max_words)
        return TranscriptTitleResult(
            title=result.answer,
            provider=result.provider,
            transcript_count=len(records),
        )

    @app.post("/v1/transcripts/translate", response_model=TranscriptTranslateResult)
    def translate_transcripts(req: TranscriptTranslateRequest) -> TranscriptTranslateResult:
        requested_ids: list[int] = []
        seen: set[int] = set()
        for raw_id in req.transcript_ids:
            try:
                item_id = int(raw_id)
            except Exception:  # noqa: BLE001
                continue
            if item_id <= 0 or item_id in seen:
                continue
            seen.add(item_id)
            requested_ids.append(item_id)

        model_name = (req.model_name or settings.ollama_model).strip() or settings.ollama_model
        target_language = str(req.target_language or "English").strip() or "English"
        source_language_raw = str(req.source_language or "").strip().lower()
        source_language = None if source_language_raw in {"", "auto", "none", "null"} else source_language_raw

        if not requested_ids:
            return TranscriptTranslateResult(
                model_name=model_name,
                source_language=source_language,
                target_language=target_language,
                items=[],
            )

        records = store.by_ids(requested_ids)
        record_map = {record.id: record for record in records}
        cached_items: dict[int, TranscriptTranslateItem] = {}
        pending: list[TranscriptRecord] = []

        with translation_cache_lock:
            for record in records:
                key = translation_cache_key(
                    record_id=record.id,
                    text=record.text,
                    model_name=model_name,
                    source_language=source_language,
                    target_language=target_language,
                )
                cached_text = translation_cache.get(key)
                if cached_text:
                    cached_items[record.id] = TranscriptTranslateItem(
                        id=record.id,
                        translated_text=cached_text,
                        cached=True,
                    )
                else:
                    pending.append(record)

        translated_items: dict[int, TranscriptTranslateItem] = {}
        if pending:
            results = qa_engine.translate_records(
                pending,
                source_language=source_language,
                target_language=target_language,
                model=model_name,
            )
            with translation_cache_lock:
                for item in results:
                    record = record_map.get(item.id)
                    if record is None:
                        continue
                    translated = str(item.translated_text or "").strip()
                    if translated:
                        key = translation_cache_key(
                            record_id=record.id,
                            text=record.text,
                            model_name=model_name,
                            source_language=source_language,
                            target_language=target_language,
                        )
                        translation_cache[key] = translated
                    translated_items[item.id] = TranscriptTranslateItem(
                        id=item.id,
                        translated_text=translated or None,
                        cached=False,
                        error=item.error,
                    )

        items: list[TranscriptTranslateItem] = []
        for item_id in requested_ids:
            cached = cached_items.get(item_id)
            if cached is not None:
                items.append(cached)
                continue
            translated = translated_items.get(item_id)
            if translated is not None:
                items.append(translated)
                continue
            if item_id not in record_map:
                items.append(TranscriptTranslateItem(id=item_id, error="Transcript id not found."))
            else:
                items.append(TranscriptTranslateItem(id=item_id, error="Translation unavailable."))

        return TranscriptTranslateResult(
            model_name=model_name,
            source_language=source_language,
            target_language=target_language,
            items=items,
        )

    @app.post("/v1/query", response_model=QueryAnswer)
    def answer_query(req: QueryRequest) -> QueryAnswer:
        limit = req.limit or settings.max_query_results
        translate_mode = bool(req.translate)
        if _is_summary_question(req.question):
            evidence_limit = min(80, max(limit * 3, 24))
            evidence = store.recent_compact(
                limit=evidence_limit,
                source_id=req.source_id,
                since_seconds=req.since_seconds,
            )
        elif translate_mode:
            candidate_limit = min(240, max(limit * 8, 64))
            candidates = store.recent(
                limit=candidate_limit,
                source_id=req.source_id,
                since_seconds=req.since_seconds,
            )
            selected_limit = min(80, max(limit * 3, 16))
            evidence = _select_translation_evidence(candidates, limit=selected_limit)
        else:
            evidence = store.search(
                query_text=req.question,
                limit=limit,
                source_id=req.source_id,
                since_seconds=req.since_seconds,
            )
        if translate_mode:
            source_hint: str | None = None
            if req.source_id:
                source_hint = transcriber.language_hint_for_source(req.source_id)
            translated_items = qa_engine.translate_records(
                evidence,
                source_language=source_hint,
                target_language="English",
                model=req.ollama_model or settings.ollama_model,
            )
            translated_lines = [
                str(item.translated_text or "").strip()
                for item in translated_items
                if str(item.translated_text or "").strip()
            ]
            answer_text = "\n".join(translated_lines).strip()
            if not answer_text:
                answer_text = "Insufficient evidence."
            result = QAResult(answer=answer_text, provider="ollama-translate")
        else:
            result = qa_engine.answer(
                req.question,
                evidence=evidence,
                provider=req.provider,
                ollama_model=req.ollama_model,
                force_translation=False,
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

    # --- Meeting detection & orchestrator endpoints ---

    @app.get("/v1/meeting/status")
    def meeting_status() -> dict[str, object]:
        return {
            "detector": meeting_detector.status(),
            "orchestrator": session_orchestrator.status(),
        }

    @app.post("/v1/meeting/detect")
    def meeting_detect() -> dict[str, object]:
        info = meeting_detector.detect()
        return {
            "active": info.active,
            "provider": info.provider,
            "title": info.title,
            "started_at": info.started_at,
            "confidence": info.confidence,
            "details": info.details,
        }

    # --- Noise suppression status ---

    @app.get("/v1/noise-suppression/status")
    def noise_suppression_status() -> dict[str, object]:
        return noise_suppressor.status()

    # --- Transcript export ---

    @app.post("/v1/transcripts/export", response_model=TranscriptExportResult)
    def export_transcripts(req: TranscriptExportRequest) -> TranscriptExportResult:
        records = store.recent(
            limit=req.limit,
            source_id=req.source_id,
            since_seconds=req.since_seconds,
        )
        if not records:
            return TranscriptExportResult(
                content="",
                format=req.format,
                transcript_count=0,
            )

        records.sort(key=lambda r: (r.started_at, r.id))
        sources = sorted(set(r.source_id for r in records))
        time_start = records[0].started_at if records else None
        time_end = records[-1].ended_at if records else None

        if req.format == "json":
            import json

            items = []
            for r in records:
                item: dict[str, object] = {
                    "id": r.id,
                    "started_at": r.started_at.isoformat(),
                    "ended_at": r.ended_at.isoformat(),
                    "text": r.text,
                }
                if req.include_speaker and r.speaker:
                    item["speaker"] = r.speaker
                if req.include_source:
                    item["source_id"] = r.source_id
                items.append(item)
            content = json.dumps(items, indent=2, ensure_ascii=False)
        elif req.format == "markdown":
            lines: list[str] = []
            lines.append("# Meeting Transcript\n")
            if time_start and time_end:
                lines.append(
                    f"**{time_start.strftime('%Y-%m-%d %H:%M')} - "
                    f"{time_end.strftime('%H:%M UTC')}**\n"
                )
            lines.append("")
            current_speaker = None
            for r in records:
                speaker = r.speaker or "Unknown"
                if req.include_speaker and speaker != current_speaker:
                    lines.append(f"\n### {speaker}\n")
                    current_speaker = speaker
                ts = r.started_at.strftime("%H:%M:%S") if req.include_timestamps else ""
                prefix = f"[{ts}] " if ts else ""
                lines.append(f"{prefix}{r.text}")
            content = "\n".join(lines)
        else:
            lines = []
            current_speaker = None
            for r in records:
                parts: list[str] = []
                if req.include_timestamps:
                    parts.append(f"[{r.started_at.strftime('%H:%M:%S')}]")
                if req.include_speaker and r.speaker:
                    if r.speaker != current_speaker:
                        parts.append(f"{r.speaker}:")
                        current_speaker = r.speaker
                if req.include_source:
                    parts.append(f"({r.source_id})")
                parts.append(r.text)
                lines.append(" ".join(parts))
            content = "\n".join(lines)

        return TranscriptExportResult(
            content=content,
            format=req.format,
            transcript_count=len(records),
            time_range_start=time_start,
            time_range_end=time_end,
            sources=sources,
        )

    # --- Capture confidence indicator ---

    @app.get("/v1/capture/confidence")
    def capture_confidence() -> dict[str, object]:
        """Meeting-aware capture confidence indicator."""
        meeting = meeting_detector.last_state
        orch_status = session_orchestrator.status()
        readiness = capture_readiness()

        # Build confidence score 0-100
        score = 0
        reasons: list[str] = []

        # Source health (40 points)
        status_str = str(readiness.get("status", ""))
        if status_str == "ready":
            score += 40
        elif status_str == "degraded":
            score += 25
            reasons.append("Capture is degraded - check system audio routing.")
        else:
            reasons.append("Capture is down - critical issues detected.")

        # Mic activity (25 points)
        signals = readiness.get("signals") or {}
        if signals.get("mic_signal_active"):
            score += 25
        else:
            reasons.append("No active mic signal detected.")

        # Transcription pipeline (20 points)
        transcriber_info = signals.get("transcriber") or {}
        queue_ratio = float(transcriber_info.get("queue_ratio") or 0)
        if queue_ratio < 0.5:
            score += 20
        elif queue_ratio < 0.8:
            score += 10
            reasons.append("Transcription queue is filling up.")
        else:
            reasons.append("Transcription queue is near capacity.")

        # System audio (15 points)
        system_count = int(signals.get("transcripts_recent_system_audio") or 0)
        if system_count > 0:
            score += 15
        else:
            reasons.append("No system audio transcripts - remote voices may not be captured.")

        # Meeting awareness bonus
        meeting_context = None
        if meeting.active:
            meeting_context = {
                "active": True,
                "provider": meeting.provider,
                "title": meeting.title,
                "orchestrator_state": orch_status.get("state"),
            }

        label = "high"
        if score < 40:
            label = "low"
        elif score < 70:
            label = "medium"

        return {
            "score": score,
            "label": label,
            "reasons": reasons,
            "meeting": meeting_context,
            "orchestrator_state": orch_status.get("state"),
        }

    @app.get("/v1/diagnostics/audio-routing")
    def audio_routing_diagnostics() -> dict[str, object]:
        """Check macOS audio device configuration for system audio routing."""
        import sounddevice as sd

        issues: list[str] = []
        recommendations: list[str] = []

        # List all available audio devices
        try:
            devices = sd.query_devices()
        except Exception:  # noqa: BLE001
            return {
                "status": "error",
                "message": "Could not query audio devices.",
                "issues": ["sounddevice.query_devices() failed"],
                "recommendations": ["Check that PortAudio is installed."],
            }

        input_devices = []
        virtual_devices = []
        virtual_keywords = ["blackhole", "cluely", "loopback", "soundflower", "virtual"]

        for i, dev in enumerate(devices):
            if dev["max_input_channels"] > 0:
                name = dev["name"]
                entry = {"index": i, "name": name, "channels": dev["max_input_channels"]}
                input_devices.append(entry)
                if any(kw in name.lower() for kw in virtual_keywords):
                    virtual_devices.append(entry)

        # Check if system-audio source is running and active
        system_source = None
        system_level = None
        for runtime in source_manager.statuses():
            if "system" in runtime.source_id.lower():
                system_source = {
                    "source_id": runtime.source_id,
                    "running": runtime.runner.is_running(),
                    "source_role": runtime.source_role,
                }
                for lvl in level_tracker.snapshot():
                    if lvl.source_id == runtime.source_id:
                        system_level = {
                            "level_dbfs": lvl.level_dbfs,
                            "updated_at": lvl.updated_at.isoformat() if lvl.updated_at else None,
                        }
                        break

        # Analyze routing health
        has_virtual_device = len(virtual_devices) > 0
        system_is_running = system_source and system_source.get("running")
        system_has_signal = system_level and (system_level.get("level_dbfs") or -96) > -80

        if not has_virtual_device:
            issues.append("No virtual audio device found (BlackHole, Cluely, Loopback).")
            recommendations.append(
                "Install BlackHole (brew install blackhole-2ch) or similar virtual audio device."
            )
        if not system_is_running:
            issues.append("System audio source is not running.")
            recommendations.append("Start system-audio capture from the console or API.")
        elif not system_has_signal:
            issues.append("System audio source is running but capturing silence.")
            recommendations.append(
                "Route macOS audio through the virtual device: "
                "System Settings > Sound > Output > select a Multi-Output Device "
                "that includes both your speakers and BlackHole/Cluely."
            )
            recommendations.append(
                "Create a Multi-Output Device in Audio MIDI Setup: "
                "open /Applications/Utilities/Audio\\ MIDI\\ Setup.app, "
                "click '+' > Create Multi-Output Device, check your speakers and BlackHole."
            )

        status = "healthy"
        if issues:
            status = "degraded" if system_is_running else "down"

        return {
            "status": status,
            "virtual_devices": virtual_devices,
            "input_device_count": len(input_devices),
            "system_source": system_source,
            "system_level": system_level,
            "issues": issues,
            "recommendations": recommendations,
        }

    return app
