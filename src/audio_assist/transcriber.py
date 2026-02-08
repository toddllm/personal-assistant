from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from threading import Lock, Thread
from queue import Empty, Full, Queue
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


class TranscriptionWorker:
    def __init__(
        self,
        store: TranscriptStore,
        model_name: str,
        device: str,
        compute_type: str,
        language: str,
        archiver: "AudioArchiver | None" = None,
        speaker_client: "SpeakerDiarizationClient | None" = None,
        mic_source_id: str = "desk-mic",
        mic_speaker_name: str | None = None,
        speaker_async_enrichment: bool = True,
        speaker_min_confidence: float = 0.55,
        speaker_queue_size: int = 1024,
    ):
        self._store = store
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._archiver = archiver
        self._speaker_client = speaker_client
        self._mic_source_id = mic_source_id
        self._mic_speaker_name = (mic_speaker_name or "").strip() or None
        self._speaker_async_enrichment = bool(speaker_async_enrichment)
        self._speaker_min_confidence = float(max(0.0, min(1.0, speaker_min_confidence)))
        self._queue: Queue[AudioSegment] = Queue(maxsize=512)
        self._speaker_queue: Queue[tuple[int, AudioSegment]] = Queue(maxsize=max(64, speaker_queue_size))
        self._thread: Thread | None = None
        self._speaker_thread: Thread | None = None
        self._running = False
        self._speaker_running = False
        self._model = None
        self._model_load_failed = False
        self._speaker_stats_lock = Lock()
        self._speaker_enqueued = 0
        self._speaker_dropped = 0
        self._speaker_processed = 0
        self._speaker_labeled = 0
        self._speaker_errors = 0
        self._speaker_low_confidence = 0

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

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._speaker_running = False
        if self._speaker_thread and self._speaker_thread.is_alive():
            self._speaker_thread.join(timeout=2)

    def enqueue(self, segment: AudioSegment) -> None:
        if not self._running:
            raise RuntimeError("Transcription worker is not running.")
        try:
            self._queue.put_nowait(segment)
        except Full:
            logger.warning("Transcription queue full, dropping segment for source=%s", segment.source_id)

    def _run(self) -> None:
        while self._running:
            try:
                segment = self._queue.get(timeout=0.4)
            except Empty:
                continue
            try:
                if self._archiver is not None:
                    self._archiver.save(segment)
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
            except Exception:  # noqa: BLE001
                logger.exception("Transcription failed for source=%s", segment.source_id)
            finally:
                self._queue.task_done()

    def _load_model(self) -> None:
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "faster-whisper is not installed correctly. Install dependencies before running."
            ) from exc
        logger.info(
            "Loading whisper model=%s device=%s compute_type=%s",
            self._model_name,
            self._device,
            self._compute_type,
        )
        self._model = WhisperModel(
            self._model_name,
            device=self._device,
            compute_type=self._compute_type,
        )
        logger.info("Whisper model loaded.")

    def _transcribe(self, segment: AudioSegment) -> str:
        if self._model is None:
            return ""
        # Convert PCM16 mono to float32 in [-1, 1] for faster-whisper.
        audio = np.frombuffer(segment.pcm_s16le, dtype=np.int16).astype(np.float32) / 32768.0
        parts, _ = self._model.transcribe(
            audio,
            language=self._language,
            vad_filter=True,
            beam_size=1,
            best_of=1,
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        return " ".join(part.text.strip() for part in parts if part.text.strip()).strip()

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
            }
