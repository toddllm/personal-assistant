from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock, Thread
from queue import Empty, Full, Queue
import time
from typing import TYPE_CHECKING

import numpy as np

from audio_assist.storage import TranscriptStore

if TYPE_CHECKING:
    from audio_assist.diarization import SpeakerDetection, SpeakerDiarizationClient
    from audio_assist.archive import AudioArchiver

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AudioSegment:
    source_id: str
    started_at: datetime
    ended_at: datetime
    pcm_s16le: bytes
    sample_rate: int
    session_id: str | None = None
    channels: int = 1


class TranscriptionWorker:
    def __init__(
        self,
        store: TranscriptStore,
        model_name: str,
        device: str,
        compute_type: str,
        language: str,
        backend: str = "faster-whisper",
        beam_size: int = 1,
        best_of: int = 1,
        vad_filter: bool = True,
        system_audio_vad_filter: bool | None = None,
        no_speech_threshold: float = 0.6,
        system_audio_no_speech_threshold: float | None = None,
        min_signal_dbfs: float = -58.0,
        system_audio_min_signal_dbfs: float | None = None,
        fallback_on_empty: bool = True,
        fallback_beam_size: int = 2,
        fallback_best_of: int = 2,
        fallback_vad_filter: bool = False,
        fallback_no_speech_threshold: float = 1.0,
        fallback_min_signal_dbfs: float = -55.0,
        system_audio_fallback_min_signal_dbfs: float | None = None,
        condition_on_previous_text: bool = False,
        queue_size: int = 512,
        archive_on_drop: bool = True,
        archiver: "AudioArchiver | None" = None,
        speaker_client: "SpeakerDiarizationClient | None" = None,
        mic_source_id: str = "desk-mic",
        mic_speaker_name: str | None = None,
        speaker_async_enrichment: bool = True,
        speaker_min_confidence: float = 0.55,
        speaker_queue_size: int = 1024,
        speaker_backfill_enabled: bool = True,
        speaker_backfill_interval_seconds: float = 20.0,
        speaker_backfill_batch_size: int = 24,
        speaker_backfill_since_seconds: int = 4 * 3600,
    ):
        self._store = store
        self._backend = backend.strip().lower() if backend else "faster-whisper"
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        self._language = self._normalize_language(language)
        self._beam_size = int(max(1, min(beam_size, 5)))
        self._best_of = int(max(1, min(best_of, 5)))
        self._vad_filter = bool(vad_filter)
        self._system_audio_vad_filter = (
            self._vad_filter if system_audio_vad_filter is None else bool(system_audio_vad_filter)
        )
        self._no_speech_threshold = float(max(0.0, min(1.0, no_speech_threshold)))
        self._system_audio_no_speech_threshold = (
            self._no_speech_threshold
            if system_audio_no_speech_threshold is None
            else float(max(0.0, min(1.0, system_audio_no_speech_threshold)))
        )
        self._min_signal_dbfs = float(min_signal_dbfs)
        self._system_audio_min_signal_dbfs = (
            self._min_signal_dbfs
            if system_audio_min_signal_dbfs is None
            else float(system_audio_min_signal_dbfs)
        )
        self._fallback_on_empty = bool(fallback_on_empty)
        self._fallback_beam_size = int(max(1, min(fallback_beam_size, 5)))
        self._fallback_best_of = int(max(1, min(fallback_best_of, 5)))
        self._fallback_vad_filter = bool(fallback_vad_filter)
        self._fallback_no_speech_threshold = float(max(0.0, min(1.0, fallback_no_speech_threshold)))
        self._fallback_min_signal_dbfs = float(fallback_min_signal_dbfs)
        self._system_audio_fallback_min_signal_dbfs = (
            self._fallback_min_signal_dbfs
            if system_audio_fallback_min_signal_dbfs is None
            else float(system_audio_fallback_min_signal_dbfs)
        )
        self._condition_on_previous_text = bool(condition_on_previous_text)
        self._archive_on_drop = bool(archive_on_drop)
        self._archiver = archiver
        self._speaker_client = speaker_client
        self._mic_source_id = mic_source_id
        self._mic_speaker_name = (mic_speaker_name or "").strip() or None
        self._speaker_async_enrichment = bool(speaker_async_enrichment)
        self._speaker_min_confidence = float(max(0.0, min(1.0, speaker_min_confidence)))
        self._speaker_backfill_enabled = bool(speaker_backfill_enabled)
        self._speaker_backfill_interval_seconds = float(max(3.0, speaker_backfill_interval_seconds))
        self._speaker_backfill_batch_size = int(max(4, min(200, speaker_backfill_batch_size)))
        self._speaker_backfill_since_seconds = int(max(60, speaker_backfill_since_seconds))
        self._queue: Queue[AudioSegment] = Queue(maxsize=max(64, int(queue_size)))
        self._speaker_queue: Queue[tuple[int, AudioSegment]] = Queue(maxsize=max(64, speaker_queue_size))
        self._thread: Thread | None = None
        self._speaker_thread: Thread | None = None
        self._speaker_backfill_thread: Thread | None = None
        self._running = False
        self._speaker_running = False
        self._speaker_backfill_running = False
        self._model = None
        self._model_load_failed = False
        self._stats_lock = Lock()
        self._segments_enqueued = 0
        self._segments_dropped = 0
        self._segments_processed = 0
        self._segments_archived = 0
        self._segments_archived_on_drop = 0
        self._segments_text = 0
        self._segments_empty = 0
        self._segments_fallback_attempted = 0
        self._segments_fallback_with_text = 0
        self._queue_high_watermark = 0
        self._speaker_stats_lock = Lock()
        self._speaker_enqueued = 0
        self._speaker_dropped = 0
        self._speaker_processed = 0
        self._speaker_labeled = 0
        self._speaker_errors = 0
        self._speaker_low_confidence = 0
        self._speaker_backfill_runs = 0
        self._speaker_backfill_scanned = 0
        self._speaker_backfill_labeled = 0
        self._speaker_backfill_missing_audio = 0
        self._speaker_backfill_errors = 0
        self._speaker_backfill_last_run_at: datetime | None = None
        self._source_language_hints: dict[str, str | None] = {}
        self._source_language_lock = Lock()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._model_load_failed = False
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()
        if self._speaker_client is not None and self._speaker_async_enrichment:
            self._speaker_running = True
            self._speaker_thread = Thread(target=self._run_speaker_enrichment, daemon=True)
            self._speaker_thread.start()
            if self._can_run_backfill():
                self._speaker_backfill_running = True
                self._speaker_backfill_thread = Thread(target=self._run_speaker_backfill_loop, daemon=True)
                self._speaker_backfill_thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._speaker_backfill_running = False
        if self._speaker_backfill_thread and self._speaker_backfill_thread.is_alive():
            self._speaker_backfill_thread.join(timeout=2)
        self._speaker_running = False
        if self._speaker_thread and self._speaker_thread.is_alive():
            self._speaker_thread.join(timeout=2)

    def enqueue(self, segment: AudioSegment) -> None:
        if not self._running:
            raise RuntimeError("Transcription worker is not running.")
        try:
            self._queue.put_nowait(segment)
            with self._stats_lock:
                self._segments_enqueued += 1
                queue_size = self._queue.qsize()
                if queue_size > self._queue_high_watermark:
                    self._queue_high_watermark = queue_size
        except Full:
            archived_on_drop = False
            if self._archive_on_drop and self._archiver is not None:
                try:
                    self._archiver.save(segment)
                    archived_on_drop = True
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Failed to archive dropped segment for source=%s started_at=%s",
                        segment.source_id,
                        segment.started_at,
                    )
            with self._stats_lock:
                self._segments_dropped += 1
                if archived_on_drop:
                    self._segments_archived_on_drop += 1
            logger.warning(
                "Transcription queue full, dropping segment for source=%s (archived_on_drop=%s)",
                segment.source_id,
                archived_on_drop,
            )

    def _run(self) -> None:
        while self._running:
            try:
                segment = self._queue.get(timeout=0.4)
            except Empty:
                continue
            try:
                if self._archiver is not None:
                    self._archiver.save(segment)
                    with self._stats_lock:
                        self._segments_archived += 1
                if self._model is None and not self._model_load_failed:
                    try:
                        self._load_model()
                    except Exception:  # noqa: BLE001
                        logger.exception("Failed to initialize whisper model. Dropping new segments.")
                        self._model_load_failed = True
                if self._model is None:
                    continue
                text = self._transcribe(segment)
                if text:
                    with self._stats_lock:
                        self._segments_text += 1
                    speaker = self._owner_speaker_for_segment(segment)
                    if speaker is None and self._speaker_client is not None and not self._speaker_async_enrichment:
                        detection = self._detect_remote_speaker(segment)
                        speaker = self._speaker_from_detection(detection)
                    record = self._store.add(
                        source_id=segment.source_id,
                        started_at=segment.started_at,
                        ended_at=segment.ended_at,
                        text=text,
                        speaker=speaker,
                        session_id=segment.session_id,
                    )
                    if (
                        speaker is None
                        and self._speaker_client is not None
                        and self._speaker_async_enrichment
                        and self._speaker_running
                    ):
                        self._enqueue_speaker_enrichment(record.id, segment)
                else:
                    with self._stats_lock:
                        self._segments_empty += 1
            except Exception:  # noqa: BLE001
                logger.exception("Transcription failed for source=%s", segment.source_id)
            finally:
                with self._stats_lock:
                    self._segments_processed += 1
                    if self._segments_processed % 50 == 0:
                        logger.info(
                            "Transcription pipeline: queue=%d/%d processed=%d text=%d empty=%d dropped=%d model=%s",
                            self._queue.qsize(),
                            self._queue.maxsize,
                            self._segments_processed,
                            self._segments_text,
                            self._segments_empty,
                            self._segments_dropped,
                            "loaded" if self._model is not None else (
                                "failed" if self._model_load_failed else "pending"
                            ),
                        )
                self._queue.task_done()

    def _load_model(self) -> None:
        if str(self._model_name).strip().lower().endswith(".en"):
            logger.warning(
                "Whisper model '%s' is English-only. Non-English speech may be mistranscribed. "
                "Use a multilingual model (for example 'base' or 'small') with language=auto.",
                self._model_name,
            )
        if self._backend == "mlx":
            self._load_model_mlx()
        else:
            self._load_model_faster_whisper()

    def _load_model_mlx(self) -> None:
        try:
            import mlx_whisper  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "mlx-whisper is not installed. Install with: pip install mlx-whisper"
            ) from exc
        # mlx-whisper model name mapping
        model_name = self._model_name
        mlx_model_map = {
            "large-v3": "mlx-community/whisper-large-v3-mlx",
            "large-v2": "mlx-community/whisper-large-v2-mlx",
            "medium": "mlx-community/whisper-medium-mlx",
            "small": "mlx-community/whisper-small-mlx",
            "base": "mlx-community/whisper-base-mlx",
            "tiny": "mlx-community/whisper-tiny-mlx",
        }
        self._mlx_model_path = mlx_model_map.get(model_name, model_name)
        logger.info(
            "Loading whisper model=%s backend=mlx (Metal GPU) mlx_path=%s",
            self._model_name,
            self._mlx_model_path,
        )
        # Warm up the model with a short silent audio to trigger download/load
        import mlx_whisper
        mlx_whisper.transcribe(
            np.zeros(16000, dtype=np.float32),
            path_or_hf_repo=self._mlx_model_path,
            language="en",
        )
        self._model = "mlx"  # sentinel; mlx-whisper is stateless
        logger.info("Whisper model loaded (mlx, Metal GPU).")

    def _load_model_faster_whisper(self) -> None:
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "faster-whisper is not installed correctly. Install dependencies before running."
            ) from exc
        logger.info(
            "Loading whisper model=%s device=%s compute_type=%s backend=faster-whisper",
            self._model_name,
            self._device,
            self._compute_type,
        )
        self._model = WhisperModel(
            self._model_name,
            device=self._device,
            compute_type=self._compute_type,
        )
        logger.info("Whisper model loaded (faster-whisper, CPU).")

    def _transcribe(self, segment: AudioSegment) -> str:
        if self._model is None:
            return ""
        # Convert PCM16 to float32 in [-1, 1], downmixing stereo to mono if needed.
        raw = np.frombuffer(segment.pcm_s16le, dtype=np.int16).astype(np.float32) / 32768.0
        if segment.channels >= 2:
            usable = raw.size - (raw.size % segment.channels)
            if usable == 0:
                return ""
            audio = raw[:usable].reshape(-1, segment.channels).mean(axis=1)
        else:
            audio = raw
        is_system_audio = segment.source_id.startswith("system-audio")
        signal_dbfs = self._signal_dbfs(audio)
        min_signal_threshold = self._system_audio_min_signal_dbfs if is_system_audio else self._min_signal_dbfs
        if signal_dbfs <= min_signal_threshold:
            return ""
        language_hint = self.language_hint_for_source(segment.source_id)
        language = language_hint if language_hint is not None else self._language
        vad_filter = self._system_audio_vad_filter if is_system_audio else self._vad_filter
        no_speech_threshold = (
            self._system_audio_no_speech_threshold if is_system_audio else self._no_speech_threshold
        )
        text = self._transcribe_once(
            audio=audio,
            language=language,
            vad_filter=vad_filter,
            beam_size=self._beam_size,
            best_of=self._best_of,
            no_speech_threshold=no_speech_threshold,
        )
        if text:
            return text
        if not self._fallback_on_empty:
            return ""
        fallback_min_signal_threshold = (
            self._system_audio_fallback_min_signal_dbfs if is_system_audio else self._fallback_min_signal_dbfs
        )
        if signal_dbfs <= fallback_min_signal_threshold:
            return ""
        with self._stats_lock:
            self._segments_fallback_attempted += 1
        fallback_text = self._transcribe_once(
            audio=audio,
            language=language,
            vad_filter=self._fallback_vad_filter,
            beam_size=self._fallback_beam_size,
            best_of=self._fallback_best_of,
            no_speech_threshold=self._fallback_no_speech_threshold,
        )
        if fallback_text:
            with self._stats_lock:
                self._segments_fallback_with_text += 1
        return fallback_text

    def _transcribe_once(
        self,
        *,
        audio: np.ndarray,
        language: str | None,
        vad_filter: bool,
        beam_size: int,
        best_of: int,
        no_speech_threshold: float,
    ) -> str:
        if self._model is None:
            return ""
        if self._backend == "mlx":
            return self._transcribe_once_mlx(
                audio=audio, language=language, beam_size=beam_size, best_of=best_of,
                no_speech_threshold=no_speech_threshold,
            )
        return self._transcribe_once_faster_whisper(
            audio=audio, language=language, vad_filter=vad_filter, beam_size=beam_size,
            best_of=best_of, no_speech_threshold=no_speech_threshold,
        )

    def _transcribe_once_mlx(
        self,
        *,
        audio: np.ndarray,
        language: str | None,
        beam_size: int,
        best_of: int,
        no_speech_threshold: float,
    ) -> str:
        import mlx_whisper
        kwargs: dict = {
            "path_or_hf_repo": self._mlx_model_path,
            "task": "transcribe",
            "no_speech_threshold": no_speech_threshold,
            "condition_on_previous_text": self._condition_on_previous_text,
            "without_timestamps": True,
        }
        # mlx-whisper does not support beam search; skip beam_size and best_of
        if language:
            kwargs["language"] = language
        result = mlx_whisper.transcribe(audio, **kwargs)
        return (result.get("text") or "").strip()

    def _transcribe_once_faster_whisper(
        self,
        *,
        audio: np.ndarray,
        language: str | None,
        vad_filter: bool,
        beam_size: int,
        best_of: int,
        no_speech_threshold: float,
    ) -> str:
        parts, _ = self._model.transcribe(
            audio,
            language=language,
            task="transcribe",
            vad_filter=vad_filter,
            beam_size=beam_size,
            best_of=best_of,
            no_speech_threshold=no_speech_threshold,
            condition_on_previous_text=self._condition_on_previous_text,
            without_timestamps=True,
        )
        return " ".join(part.text.strip() for part in parts if part.text.strip()).strip()

    @staticmethod
    def _signal_dbfs(audio: np.ndarray) -> float:
        if audio.size == 0:
            return -96.0
        rms = float(np.sqrt(np.mean(audio * audio)))
        if rms <= 1e-9:
            return -96.0
        return float(max(-96.0, 20.0 * np.log10(rms)))

    @staticmethod
    def _normalize_language(language: str | None) -> str | None:
        value = str(language or "").strip().lower()
        if not value or value in {"auto", "none", "null"}:
            return None
        return value

    def set_source_language_hint(self, source_id: str, language_hint: str | None) -> str | None:
        key = str(source_id or "").strip()
        if not key:
            return None
        normalized = self._normalize_language(language_hint)
        with self._source_language_lock:
            if normalized is None:
                self._source_language_hints.pop(key, None)
            else:
                self._source_language_hints[key] = normalized
        return normalized

    def language_hint_for_source(self, source_id: str) -> str | None:
        key = str(source_id or "").strip()
        if not key:
            return None
        with self._source_language_lock:
            return self._source_language_hints.get(key)

    def transcription_pipeline_status(self) -> dict[str, object]:
        with self._stats_lock:
            return {
                "queue_size": self._queue.qsize(),
                "queue_capacity": self._queue.maxsize,
                "queue_high_watermark": self._queue_high_watermark,
                "enqueued": self._segments_enqueued,
                "dropped": self._segments_dropped,
                "processed": self._segments_processed,
                "archived": self._segments_archived,
                "archived_on_drop": self._segments_archived_on_drop,
                "with_text": self._segments_text,
                "empty_text": self._segments_empty,
                "fallback_attempted": self._segments_fallback_attempted,
                "fallback_with_text": self._segments_fallback_with_text,
                "model_name": self._model_name,
                "language_default": self._language or "auto",
                "beam_size": self._beam_size,
                "best_of": self._best_of,
                "vad_filter_default": self._vad_filter,
                "vad_filter_system_audio": self._system_audio_vad_filter,
                "no_speech_threshold_default": self._no_speech_threshold,
                "no_speech_threshold_system_audio": self._system_audio_no_speech_threshold,
                "min_signal_dbfs_default": self._min_signal_dbfs,
                "min_signal_dbfs_system_audio": self._system_audio_min_signal_dbfs,
                "fallback_enabled": self._fallback_on_empty,
                "fallback_beam_size": self._fallback_beam_size,
                "fallback_best_of": self._fallback_best_of,
                "fallback_vad_filter": self._fallback_vad_filter,
                "fallback_no_speech_threshold": self._fallback_no_speech_threshold,
                "fallback_min_signal_dbfs_default": self._fallback_min_signal_dbfs,
                "fallback_min_signal_dbfs_system_audio": self._system_audio_fallback_min_signal_dbfs,
            }

    def _owner_speaker_for_segment(self, segment: AudioSegment) -> str | None:
        if self._mic_speaker_name and segment.source_id == self._mic_source_id:
            return self._mic_speaker_name
        return None

    def _detect_remote_speaker(self, segment: AudioSegment) -> "SpeakerDetection | None":
        if self._speaker_client is None:
            return None
        try:
            return self._speaker_client.detect(segment)
        except Exception:  # noqa: BLE001
            logger.exception("Speaker detection failed for source=%s", segment.source_id)
            return None

    def _speaker_from_detection(self, detection: "SpeakerDetection | None") -> str | None:
        if detection is None:
            return None
        speaker = str(detection.speaker or "").strip()
        if not speaker:
            return None
        confidence = detection.confidence
        if confidence is not None and confidence < self._speaker_min_confidence:
            with self._speaker_stats_lock:
                self._speaker_low_confidence += 1
            return None
        return speaker[:64]

    def _enqueue_speaker_enrichment(self, transcript_id: int, segment: AudioSegment) -> None:
        try:
            self._speaker_queue.put_nowait((transcript_id, segment))
            with self._speaker_stats_lock:
                self._speaker_enqueued += 1
        except Full:
            with self._speaker_stats_lock:
                self._speaker_dropped += 1
            logger.warning(
                "Speaker enrichment queue full, dropping transcript_id=%s source=%s",
                transcript_id,
                segment.source_id,
            )

    def _run_speaker_enrichment(self) -> None:
        while self._speaker_running:
            try:
                transcript_id, segment = self._speaker_queue.get(timeout=0.4)
            except Empty:
                continue
            try:
                detection = self._detect_remote_speaker(segment)
                speaker = self._speaker_from_detection(detection)
                if speaker:
                    updated = self._store.update_speaker(transcript_id, speaker)
                    with self._speaker_stats_lock:
                        self._speaker_processed += 1
                        if updated:
                            self._speaker_labeled += 1
                else:
                    with self._speaker_stats_lock:
                        self._speaker_processed += 1
            except Exception:  # noqa: BLE001
                with self._speaker_stats_lock:
                    self._speaker_errors += 1
                logger.exception(
                    "Speaker enrichment failed for transcript_id=%s source=%s",
                    transcript_id,
                    segment.source_id,
                )
            finally:
                self._speaker_queue.task_done()

    def _can_run_backfill(self) -> bool:
        return (
            self._speaker_client is not None
            and self._speaker_async_enrichment
            and self._speaker_backfill_enabled
            and self._archiver is not None
        )

    def _run_speaker_backfill_loop(self) -> None:
        while self._speaker_backfill_running:
            try:
                # Prioritize live queue first.
                if self._speaker_queue.qsize() >= max(24, self._speaker_backfill_batch_size):
                    time.sleep(1.5)
                    continue
                self.run_speaker_backfill_once()
            except Exception:  # noqa: BLE001
                with self._speaker_stats_lock:
                    self._speaker_backfill_errors += 1
                logger.exception("Speaker backfill loop failed.")
            time.sleep(self._speaker_backfill_interval_seconds)

    def run_speaker_backfill_once(self, limit: int | None = None, since_seconds: int | None = None) -> dict[str, int]:
        if not self._can_run_backfill():
            return {
                "runs": 0,
                "scanned": 0,
                "labeled": 0,
                "missing_audio": 0,
                "errors": 0,
            }
        if self._archiver is None:
            return {
                "runs": 0,
                "scanned": 0,
                "labeled": 0,
                "missing_audio": 0,
                "errors": 0,
            }

        batch_limit = int(max(1, min(limit or self._speaker_backfill_batch_size, 500)))
        since = int(max(60, since_seconds or self._speaker_backfill_since_seconds))
        scanned = 0
        labeled = 0
        missing_audio = 0
        errors = 0

        records = self._store.recent_unlabeled(limit=batch_limit, since_seconds=since)
        for record in records:
            if record.source_id == self._mic_source_id and self._mic_speaker_name:
                continue
            loaded = self._archiver.load_segment(
                source_id=record.source_id,
                started_at=record.started_at,
                ended_at=record.ended_at,
            )
            if loaded is None:
                missing_audio += 1
                continue
            pcm, sample_rate = loaded
            segment = AudioSegment(
                source_id=record.source_id,
                session_id=record.session_id,
                started_at=record.started_at,
                ended_at=record.ended_at,
                pcm_s16le=pcm,
                sample_rate=sample_rate,
            )
            scanned += 1
            try:
                detection = self._detect_remote_speaker(segment)
                speaker = self._speaker_from_detection(detection)
                if speaker and self._store.update_speaker(record.id, speaker):
                    labeled += 1
            except Exception:  # noqa: BLE001
                errors += 1
                logger.exception(
                    "Speaker backfill failed for transcript_id=%s source=%s",
                    record.id,
                    record.source_id,
                )

        now = datetime.now(tz=UTC)
        with self._speaker_stats_lock:
            self._speaker_backfill_runs += 1
            self._speaker_backfill_scanned += scanned
            self._speaker_backfill_labeled += labeled
            self._speaker_backfill_missing_audio += missing_audio
            self._speaker_backfill_errors += errors
            self._speaker_backfill_last_run_at = now
        return {
            "runs": 1,
            "scanned": scanned,
            "labeled": labeled,
            "missing_audio": missing_audio,
            "errors": errors,
        }

    def speaker_pipeline_status(self) -> dict[str, object]:
        mode = "off"
        if self._speaker_client is not None:
            mode = "async" if self._speaker_async_enrichment else "inline"
        with self._speaker_stats_lock:
            return {
                "mode": mode,
                "queue_size": self._speaker_queue.qsize() if mode == "async" else 0,
                "enqueued": self._speaker_enqueued,
                "processed": self._speaker_processed,
                "labeled": self._speaker_labeled,
                "dropped": self._speaker_dropped,
                "errors": self._speaker_errors,
                "low_confidence": self._speaker_low_confidence,
                "min_confidence": self._speaker_min_confidence,
                "backfill_enabled": self._can_run_backfill(),
                "backfill_running": self._speaker_backfill_running,
                "backfill_runs": self._speaker_backfill_runs,
                "backfill_scanned": self._speaker_backfill_scanned,
                "backfill_labeled": self._speaker_backfill_labeled,
                "backfill_missing_audio": self._speaker_backfill_missing_audio,
                "backfill_errors": self._speaker_backfill_errors,
                "backfill_last_run_at": self._speaker_backfill_last_run_at,
            }
