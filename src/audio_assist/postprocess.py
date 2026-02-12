from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import logging
from threading import Lock
import time

import numpy as np

from audio_assist.archive import AudioArchiver
from audio_assist.schemas import (
    TranscriptPostprocessItem,
    TranscriptPostprocessRequest,
    TranscriptPostprocessResult,
)
from audio_assist.storage import TranscriptRecord, TranscriptStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _TranscribeResult:
    text: str
    detected_language: str | None = None
    language_probability: float | None = None


@dataclass(slots=True)
class _RecordAnalysis:
    record: TranscriptRecord
    missing_audio: bool = False
    error: str | None = None
    transcribed: _TranscribeResult | None = None


class TranscriptPostProcessor:
    def __init__(
        self,
        store: TranscriptStore,
        archiver: AudioArchiver,
        default_model_name: str,
        default_device: str,
        default_compute_type: str,
        default_language: str | None = "auto",
        default_cpu_threads: int = 0,
        default_num_workers: int = 2,
        default_parallelism: int = 2,
        default_beam_size: int = 1,
        default_best_of: int = 1,
        default_vad_filter: bool = True,
        default_no_speech_threshold: float = 1.0,
    ):
        self._store = store
        self._archiver = archiver
        self._default_model_name = str(default_model_name or "small").strip()
        self._default_device = str(default_device or "cpu").strip()
        self._default_compute_type = str(default_compute_type or "int8").strip()
        self._default_language = self._normalize_language(default_language)
        self._default_cpu_threads = max(0, int(default_cpu_threads))
        self._default_num_workers = max(1, int(default_num_workers))
        self._default_parallelism = max(1, min(int(default_parallelism), 8))
        self._default_beam_size = max(1, min(int(default_beam_size), 5))
        self._default_best_of = max(1, min(int(default_best_of), 5))
        self._default_vad_filter = bool(default_vad_filter)
        self._default_no_speech_threshold = float(max(0.0, min(1.0, default_no_speech_threshold)))
        self._model_lock = Lock()
        self._model_name_loaded: str | None = None
        self._model = None

    def process_recent(self, req: TranscriptPostprocessRequest) -> TranscriptPostprocessResult:
        started = time.perf_counter()
        model_name = (req.model_name or self._default_model_name).strip()
        if not model_name:
            model_name = self._default_model_name
        language = self._normalize_language(req.language)
        if req.language is None:
            language = self._default_language
        parallelism = self._clamp_int(req.parallelism, self._default_parallelism, 1, 8)
        beam_size = self._clamp_int(req.beam_size, self._default_beam_size, 1, 5)
        best_of = self._clamp_int(req.best_of, self._default_best_of, 1, 5)
        vad_filter = self._default_vad_filter if req.vad_filter is None else bool(req.vad_filter)
        no_speech_threshold = self._clamp_float(
            req.no_speech_threshold,
            self._default_no_speech_threshold,
            0.0,
            1.0,
        )

        records = self._store.recent(
            limit=req.limit,
            source_id=req.source_id,
            since_seconds=req.since_seconds,
        )
        if records:
            # Ensure model is loaded before optional parallel work starts.
            self._get_model(model_name)

        scanned = 0
        changed = 0
        applied = 0
        missing_audio = 0
        failed = 0
        items: list[TranscriptPostprocessItem] = []
        analyses = self._analyze_records(
            records=records,
            model_name=model_name,
            language=language,
            beam_size=beam_size,
            best_of=best_of,
            vad_filter=vad_filter,
            no_speech_threshold=no_speech_threshold,
            parallelism=parallelism,
        )

        for analysis in analyses:
            scanned += 1
            record = analysis.record
            if analysis.missing_audio:
                missing_audio += 1
                items.append(
                    TranscriptPostprocessItem(
                        id=record.id,
                        source_id=record.source_id,
                        started_at=record.started_at,
                        ended_at=record.ended_at,
                        old_text=record.text,
                        error="Archived WAV chunk not found for this record.",
                    )
                )
                continue
            if analysis.error is not None:
                failed += 1
                items.append(
                    TranscriptPostprocessItem(
                        id=record.id,
                        source_id=record.source_id,
                        started_at=record.started_at,
                        ended_at=record.ended_at,
                        old_text=record.text,
                        error=analysis.error,
                    )
                )
                continue

            transcribed = analysis.transcribed
            if transcribed is None:
                failed += 1
                items.append(
                    TranscriptPostprocessItem(
                        id=record.id,
                        source_id=record.source_id,
                        started_at=record.started_at,
                        ended_at=record.ended_at,
                        old_text=record.text,
                        error="Postprocess transcription returned no result.",
                    )
                )
                continue

            new_text = transcribed.text.strip()
            old_text = record.text.strip()
            is_changed = bool(new_text) and (new_text != old_text)
            if is_changed:
                changed += 1
            did_apply = False
            if req.apply and is_changed:
                did_apply = self._store.update_text(record.id, new_text)
                if did_apply:
                    applied += 1

            if req.include_unchanged or is_changed or did_apply:
                items.append(
                    TranscriptPostprocessItem(
                        id=record.id,
                        source_id=record.source_id,
                        started_at=record.started_at,
                        ended_at=record.ended_at,
                        old_text=record.text,
                        new_text=new_text or None,
                        changed=is_changed,
                        applied=did_apply,
                        detected_language=transcribed.detected_language,
                        language_probability=transcribed.language_probability,
                    )
                )

        elapsed_seconds = max(time.perf_counter() - started, 0.0001)
        elapsed_ms = int(round(elapsed_seconds * 1000.0))
        avg_ms_per_chunk = (elapsed_ms / scanned) if scanned > 0 else None
        chunks_per_second = (scanned / elapsed_seconds) if scanned > 0 else None

        return TranscriptPostprocessResult(
            scanned=scanned,
            changed=changed,
            applied=applied,
            missing_audio=missing_audio,
            failed=failed,
            model_name=model_name,
            language=language or "auto",
            elapsed_ms=elapsed_ms,
            avg_ms_per_chunk=avg_ms_per_chunk,
            chunks_per_second=chunks_per_second,
            items=items,
        )

    def _analyze_records(
        self,
        records: list[TranscriptRecord],
        model_name: str,
        language: str | None,
        beam_size: int,
        best_of: int,
        vad_filter: bool,
        no_speech_threshold: float,
        parallelism: int,
    ) -> list[_RecordAnalysis]:
        if not records:
            return []
        if parallelism <= 1 or len(records) <= 1:
            return [
                self._analyze_record(
                    record=record,
                    model_name=model_name,
                    language=language,
                    beam_size=beam_size,
                    best_of=best_of,
                    vad_filter=vad_filter,
                    no_speech_threshold=no_speech_threshold,
                )
                for record in records
            ]

        results: list[_RecordAnalysis | None] = [None] * len(records)
        with ThreadPoolExecutor(max_workers=parallelism, thread_name_prefix="postprocess") as pool:
            future_to_idx = {
                pool.submit(
                    self._analyze_record,
                    record=record,
                    model_name=model_name,
                    language=language,
                    beam_size=beam_size,
                    best_of=best_of,
                    vad_filter=vad_filter,
                    no_speech_threshold=no_speech_threshold,
                ): idx
                for idx, record in enumerate(records)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception as exc:  # noqa: BLE001
                    record = records[idx]
                    logger.exception("Transcript post-processing failed for transcript_id=%s", record.id)
                    results[idx] = _RecordAnalysis(record=record, error=str(exc))
        return [item for item in results if item is not None]

    def _analyze_record(
        self,
        record: TranscriptRecord,
        model_name: str,
        language: str | None,
        beam_size: int,
        best_of: int,
        vad_filter: bool,
        no_speech_threshold: float,
    ) -> _RecordAnalysis:
        loaded = self._archiver.load_segment(
            source_id=record.source_id,
            started_at=record.started_at,
            ended_at=record.ended_at,
        )
        if loaded is None:
            return _RecordAnalysis(record=record, missing_audio=True)

        pcm, sample_rate = loaded
        try:
            transcribed = self._transcribe_chunk(
                raw_pcm=pcm,
                sample_rate=sample_rate,
                model_name=model_name,
                language=language,
                beam_size=beam_size,
                best_of=best_of,
                vad_filter=vad_filter,
                no_speech_threshold=no_speech_threshold,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Transcript post-processing failed for transcript_id=%s", record.id)
            return _RecordAnalysis(record=record, error=str(exc))
        return _RecordAnalysis(record=record, transcribed=transcribed)

    def _load_model(self, model_name: str):
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "faster-whisper is not installed correctly. Install dependencies before post-processing."
            ) from exc

        logger.info(
            "Loading postprocess whisper model=%s device=%s compute_type=%s cpu_threads=%s num_workers=%s",
            model_name,
            self._default_device,
            self._default_compute_type,
            self._default_cpu_threads,
            self._default_num_workers,
        )
        kwargs: dict[str, object] = {
            "device": self._default_device,
            "compute_type": self._default_compute_type,
        }
        if self._default_cpu_threads > 0:
            kwargs["cpu_threads"] = self._default_cpu_threads
        if self._default_num_workers > 1:
            kwargs["num_workers"] = self._default_num_workers

        try:
            self._model = WhisperModel(model_name, **kwargs)
        except TypeError:
            # Backward compatibility with older faster-whisper builds.
            kwargs.pop("cpu_threads", None)
            kwargs.pop("num_workers", None)
            self._model = WhisperModel(model_name, **kwargs)
            logger.warning("Postprocess WhisperModel does not support cpu_threads/num_workers; using defaults.")
        self._model_name_loaded = model_name
        logger.info("Postprocess whisper model loaded.")
        return self._model

    def _get_model(self, model_name: str):
        with self._model_lock:
            if self._model is not None and self._model_name_loaded == model_name:
                return self._model
            return self._load_model(model_name)

    def _transcribe_chunk(
        self,
        raw_pcm: bytes,
        sample_rate: int,
        model_name: str,
        language: str | None,
        beam_size: int,
        best_of: int,
        vad_filter: bool,
        no_speech_threshold: float,
    ) -> _TranscribeResult:
        if not raw_pcm:
            return _TranscribeResult(text="")
        model = self._get_model(model_name)
        audio = np.frombuffer(raw_pcm, dtype=np.int16).astype(np.float32) / 32768.0
        lang = self._normalize_language(language)
        parts, info = model.transcribe(
            audio,
            language=lang,
            vad_filter=vad_filter,
            beam_size=beam_size,
            best_of=best_of,
            no_speech_threshold=no_speech_threshold,
            condition_on_previous_text=False,
            without_timestamps=True,
            task="transcribe",
        )
        text = " ".join(part.text.strip() for part in parts if part.text.strip()).strip()
        detected_language = getattr(info, "language", None)
        probability_raw = getattr(info, "language_probability", None)
        language_probability = float(probability_raw) if isinstance(probability_raw, (float, int)) else None
        return _TranscribeResult(
            text=text,
            detected_language=str(detected_language).strip() if detected_language else None,
            language_probability=language_probability,
        )

    @staticmethod
    def _normalize_language(language: str | None) -> str | None:
        if language is None:
            return None
        value = str(language).strip().lower()
        if not value or value in {"auto", "none", "null"}:
            return None
        return value

    @staticmethod
    def _clamp_int(value: int | None, fallback: int, min_value: int, max_value: int) -> int:
        if value is None:
            return int(max(min(fallback, max_value), min_value))
        return int(max(min(value, max_value), min_value))

    @staticmethod
    def _clamp_float(value: float | None, fallback: float, min_value: float, max_value: float) -> float:
        if value is None:
            return float(max(min(fallback, max_value), min_value))
        return float(max(min(value, max_value), min_value))
