from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging

import httpx

from audio_assist.transcriber import AudioSegment

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SpeakerDetection:
    speaker: str | None
    confidence: float | None = None


class SpeakerDiarizationClient:
    """Best-effort speaker labeling against an optional external microservice."""

    def __init__(
        self,
        enabled: bool,
        base_url: str,
        timeout_seconds: float,
        cooldown_seconds: float = 30.0,
    ):
        self._enabled = enabled
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = max(0.2, timeout_seconds)
        self._cooldown = timedelta(seconds=max(1.0, cooldown_seconds))
        self._retry_after: datetime | None = None
        self._last_error: str | None = None
        self._logged_unavailable = False
        self._last_health_probe_at: datetime | None = None
        self._last_health_probe_ok = False

    def status(self) -> dict[str, object]:
        now = datetime.now(tz=UTC)
        if self._enabled and (self._retry_after is None or now >= self._retry_after):
            self._probe_health(now)
            now = datetime.now(tz=UTC)
        cooling_down = self._retry_after is not None and now < self._retry_after
        return {
            "enabled": self._enabled,
            "reachable": self._enabled and not cooling_down and self._last_health_probe_ok,
            "cooldown_active": cooling_down,
            "retry_after": self._retry_after,
            "error": self._last_error,
            "base_url": self._base_url,
        }

    def detect(self, segment: AudioSegment) -> SpeakerDetection:
        if not self._enabled:
            return SpeakerDetection(speaker=None, confidence=None)
        now = datetime.now(tz=UTC)
        if self._retry_after is not None and now < self._retry_after:
            return SpeakerDetection(speaker=None, confidence=None)
        if not segment.pcm_s16le:
            return SpeakerDetection(speaker=None, confidence=None)

        payload = {
            "source_id": segment.source_id,
            "session_id": segment.session_id,
            "sample_rate": segment.sample_rate,
            "started_at": segment.started_at.astimezone(UTC).isoformat(),
            "ended_at": segment.ended_at.astimezone(UTC).isoformat(),
            "pcm_s16le_base64": base64.b64encode(segment.pcm_s16le).decode("ascii"),
        }
        try:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.post(f"{self._base_url}/v1/diarize/chunk", json=payload)
            if response.status_code >= 400:
                detail = self._extract_error(response)
                self._mark_failure(detail)
                return SpeakerDetection(speaker=None, confidence=None)
            data = response.json()
            self._last_error = None
            self._retry_after = None
            self._logged_unavailable = False
            self._last_health_probe_ok = True
            self._last_health_probe_at = datetime.now(tz=UTC)
            return self._extract_speaker(data)
        except Exception as exc:  # noqa: BLE001
            self._mark_failure(str(exc))
            return SpeakerDetection(speaker=None, confidence=None)

    def _mark_failure(self, error: str) -> None:
        self._last_error = error
        self._retry_after = datetime.now(tz=UTC) + self._cooldown
        self._last_health_probe_ok = False
        self._last_health_probe_at = datetime.now(tz=UTC)
        if not self._logged_unavailable:
            logger.warning("Speaker diarization unavailable, continuing without labels: %s", error)
            self._logged_unavailable = True

    def _probe_health(self, now: datetime) -> None:
        if self._last_health_probe_at is not None and (now - self._last_health_probe_at) < timedelta(seconds=10):
            return
        self._last_health_probe_at = now
        probe_timeout = min(1.2, self._timeout_seconds)
        try:
            with httpx.Client(timeout=probe_timeout) as client:
                response = client.get(f"{self._base_url}/health")
            if response.status_code >= 400:
                self._mark_failure(self._extract_error(response))
                return
            self._last_error = None
            self._retry_after = None
            self._last_health_probe_ok = True
            self._logged_unavailable = False
        except Exception as exc:  # noqa: BLE001
            self._mark_failure(str(exc))

    @staticmethod
    def _extract_error(response: httpx.Response) -> str:
        try:
            payload = response.json()
            detail = payload.get("detail")
            if detail:
                return str(detail)
        except Exception:  # noqa: BLE001
            pass
        text = response.text.strip()
        if text:
            return text
        return f"HTTP {response.status_code}"

    @staticmethod
    def _normalize_speaker(raw: object) -> str | None:
        speaker = str(raw or "").strip()
        if not speaker:
            return None
        return speaker[:64]

    def _extract_speaker(self, payload: dict[str, object]) -> SpeakerDetection:
        direct = self._normalize_speaker(payload.get("speaker"))
        if direct:
            conf = payload.get("confidence")
            confidence = float(conf) if isinstance(conf, (int, float)) else None
            return SpeakerDetection(speaker=direct, confidence=confidence)

        segments = payload.get("segments")
        if not isinstance(segments, list):
            return SpeakerDetection(speaker=None, confidence=None)

        best_speaker = None
        best_conf = None
        best_score = -1.0
        for item in segments:
            if not isinstance(item, dict):
                continue
            speaker = self._normalize_speaker(item.get("speaker"))
            if not speaker:
                continue
            confidence_raw = item.get("confidence")
            confidence = float(confidence_raw) if isinstance(confidence_raw, (int, float)) else 0.5
            duration = self._segment_duration(item)
            score = max(0.01, duration) * max(0.0, confidence)
            if score > best_score:
                best_score = score
                best_speaker = speaker
                best_conf = confidence if isinstance(confidence_raw, (int, float)) else None
        return SpeakerDetection(speaker=best_speaker, confidence=best_conf)

    @staticmethod
    def _segment_duration(segment: dict[str, object]) -> float:
        duration = segment.get("duration")
        if isinstance(duration, (int, float)):
            return float(duration)
        start = segment.get("start")
        end = segment.get("end")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            return max(0.0, float(end) - float(start))
        return 0.0
