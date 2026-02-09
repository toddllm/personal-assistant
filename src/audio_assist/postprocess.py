from __future__ import annotations

from dataclasses import dataclass
import logging
from threading import Lock

import numpy as np

from audio_assist.archive import AudioArchiver
from audio_assist.schemas import (
    TranscriptPostprocessItem,
    TranscriptPostprocessRequest,
    TranscriptPostprocessResult,
)
from audio_assist.storage import TranscriptStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _TranscribeResult:
    text: str
    detected_language: str | None = None
    language_probability: float | None = None


class TranscriptPostProcessor:
    def __init__(
        self,
        store: TranscriptStore,
        archiver: AudioArchiver,
        default_model_name: str,
        default_device: str,
        default_compute_type: str,
        default_language: str | None = "auto",
    ):
        self._store = store
        self._archiver = archiver
        self._default_model_name = str(default_model_name or "small").strip()
        self._default_device = str(default_device or "cpu").strip()
        self._default_compute_type = str(default_compute_type or "int8").strip()
        self._default_language = self._normalize_language(default_language)
        self._model_lock = Lock()
        self._model_name_loaded: str | None = None
        self._model = None

    def process_recent(self, req: TranscriptPostprocessRequest) -> TranscriptPostprocessResult:
        model_name = (req.model_name or self._default_model_name).strip()
        if not model_name:
            model_name = self._default_model_name
        language = self._normalize_language(req.language)
        if req.language is None:
            language = self._default_language

        records = self._store.recent(
            limit=req.limit,
            source_id=req.source_id,
            since_seconds=req.since_seconds,
        )

        scanned = 0
        changed = 0
        applied = 0
        missing_audio = 0
        failed = 0
        items: list[TranscriptPostprocessItem] = []

        for record in records:
            scanned += 1
            loaded = self._archiver.load_segment(
                source_id=record.source_id,
                started_at=record.started_at,
                ended_at=record.ended_at,
            )
            if loaded is None:
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

            pcm, sample_rate = loaded
            try:
                transcribed = self._transcribe_chunk(
                    raw_pcm=pcm,
                    sample_rate=sample_rate,
                    model_name=model_name,
                    language=language,
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.exception("Transcript post-processing failed for transcript_id=%s", record.id)
                items.append(
                    TranscriptPostprocessItem(
                        id=record.id,
                        source_id=record.source_id,
                        started_at=record.started_at,
                        ended_at=record.ended_at,
                        old_text=record.text,
                        error=str(exc),
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

        return TranscriptPostprocessResult(
            scanned=scanned,
            changed=changed,
            applied=applied,
            missing_audio=missing_audio,
            failed=failed,
            model_name=model_name,
            language=language or "auto",
            items=items,
        )

    def _load_model(self, model_name: str):
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "faster-whisper is not installed correctly. Install dependencies before post-processing."
            ) from exc

        logger.info(
            "Loading postprocess whisper model=%s device=%s compute_type=%s",
            model_name,
            self._default_device,
            self._default_compute_type,
        )
        self._model = WhisperModel(
            model_name,
            device=self._default_device,
            compute_type=self._default_compute_type,
        )
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
    ) -> _TranscribeResult:
        if not raw_pcm:
            return _TranscribeResult(text="")
        model = self._get_model(model_name)
        audio = np.frombuffer(raw_pcm, dtype=np.int16).astype(np.float32) / 32768.0
        lang = self._normalize_language(language)
        parts, info = model.transcribe(
            audio,
            language=lang,
            vad_filter=True,
            beam_size=2,
            best_of=2,
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

