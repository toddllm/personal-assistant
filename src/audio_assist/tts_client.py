from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Iterator

import httpx

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TTSResult:
    audio_base64: str | None
    mime_type: str | None
    provider: str | None
    error: str | None


class TTSClient:
    def __init__(
        self,
        enabled: bool,
        base_url: str,
        timeout_seconds: float,
        default_voice: str,
        default_language: str,
        default_instruct: str | None,
    ):
        self._enabled = enabled
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._default_voice = default_voice
        self._default_language = default_language
        self._default_instruct = default_instruct

    @staticmethod
    def _sse_event(event: str, payload: dict[str, object]) -> bytes:
        return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    @staticmethod
    def _extract_error_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
            detail = payload.get("detail")
            if detail:
                return str(detail)
        except Exception:  # noqa: BLE001
            pass
        body = response.text.strip()
        if body:
            return body
        return f"HTTP {response.status_code} from TTS service."

    def status(self) -> dict[str, object]:
        if not self._enabled:
            return {
                "enabled": False,
                "reachable": False,
                "provider": None,
                "model_loaded": False,
                "error": "TTS disabled by configuration.",
            }
        try:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.get(f"{self._base_url}/health")
                response.raise_for_status()
                payload = response.json()
            return {
                "enabled": True,
                "reachable": True,
                "provider": payload.get("provider"),
                "model_loaded": bool(payload.get("model_loaded", False)),
                "error": payload.get("load_error"),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "enabled": True,
                "reachable": False,
                "provider": None,
                "model_loaded": False,
                "error": str(exc),
            }

    def voices(self) -> dict[str, object]:
        if not self._enabled:
            return {
                "enabled": False,
                "reachable": False,
                "voices": [self._default_voice],
                "default_voice": self._default_voice,
                "error": "TTS disabled by configuration.",
            }
        try:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.get(f"{self._base_url}/v1/voices")
                response.raise_for_status()
                payload = response.json()
            return {
                "enabled": True,
                "reachable": True,
                "voices": payload.get("voices", [self._default_voice]) or [self._default_voice],
                "default_voice": self._default_voice,
                "provider": payload.get("provider"),
                "model_loaded": bool(payload.get("model_loaded", False)),
                "error": payload.get("load_error"),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "enabled": True,
                "reachable": False,
                "voices": [self._default_voice],
                "default_voice": self._default_voice,
                "provider": None,
                "model_loaded": False,
                "error": str(exc),
            }

    def synthesize(
        self,
        text: str,
        voice: str | None = None,
        language: str | None = None,
        instruct: str | None = None,
    ) -> TTSResult:
        if not self._enabled:
            return TTSResult(
                audio_base64=None,
                mime_type=None,
                provider=None,
                error="TTS disabled by configuration.",
            )
        normalized_text = text.strip()
        if not normalized_text:
            return TTSResult(
                audio_base64=None,
                mime_type=None,
                provider=None,
                error="TTS text was empty after normalization.",
            )

        selected_voice = voice or self._default_voice
        selected_language = language or self._default_language
        selected_instruct = instruct if instruct is not None else self._default_instruct
        request_timeout = self._timeout_seconds

        for attempt in range(2):
            payload = {
                "text": normalized_text,
                "voice": selected_voice,
                "language": selected_language,
                "instruct": selected_instruct,
                "save_audio": False,
            }
            try:
                with httpx.Client(timeout=request_timeout) as client:
                    response = client.post(f"{self._base_url}/v1/synthesize", json=payload)
                if response.status_code >= 400:
                    detail = self._extract_error_detail(response)
                    logger.warning(
                        "TTS synth request failed with status=%s detail=%s",
                        response.status_code,
                        detail,
                    )
                    return TTSResult(
                        audio_base64=None,
                        mime_type=None,
                        provider=None,
                        error=detail,
                    )

                data = response.json()
                audio_base64 = data.get("audio_base64")
                if not isinstance(audio_base64, str) or not audio_base64:
                    provider = str(data.get("provider")) if data.get("provider") else None
                    logger.warning(
                        "TTS synth response missing audio payload. provider=%s", provider
                    )
                    return TTSResult(
                        audio_base64=None,
                        mime_type=None,
                        provider=provider,
                        error="TTS response did not include audio payload.",
                    )
                return TTSResult(
                    audio_base64=audio_base64,
                    mime_type=str(data.get("mime_type", "audio/wav")),
                    provider=str(data.get("provider", "tts-service")),
                    error=None,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                if attempt == 0:
                    request_timeout = max(self._timeout_seconds * 2.0, self._timeout_seconds + 10.0)
                    logger.warning(
                        "TTS synth request failed (%s). Retrying once with timeout %.1fs.",
                        exc.__class__.__name__,
                        request_timeout,
                    )
                    continue
                logger.warning("TTS synth request failed after retry: %s", exc)
                return TTSResult(
                    audio_base64=None,
                    mime_type=None,
                    provider=None,
                    error=str(exc),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("TTS synth unexpected failure: %s", exc)
                return TTSResult(
                    audio_base64=None,
                    mime_type=None,
                    provider=None,
                    error=str(exc),
                )

        return TTSResult(
            audio_base64=None,
            mime_type=None,
            provider=None,
            error="TTS synthesis failed after retries.",
        )

    def stream_synthesize(
        self,
        text: str,
        voice: str | None = None,
        language: str | None = None,
        instruct: str | None = None,
    ) -> Iterator[bytes]:
        if not self._enabled:
            return iter(
                [
                    self._sse_event("error", {"error": "TTS disabled by configuration."}),
                    self._sse_event("done", {"ok": False}),
                ]
            )

        normalized_text = text.strip()
        if not normalized_text:
            return iter(
                [
                    self._sse_event("error", {"error": "TTS text was empty after normalization."}),
                    self._sse_event("done", {"ok": False}),
                ]
            )

        selected_voice = voice or self._default_voice
        selected_language = language or self._default_language
        selected_instruct = instruct if instruct is not None else self._default_instruct

        def _iter() -> Iterator[bytes]:
            payload = {
                "text": normalized_text,
                "voice": selected_voice,
                "language": selected_language,
                "instruct": selected_instruct,
                "save_audio": False,
            }
            timeout = httpx.Timeout(
                connect=self._timeout_seconds,
                read=None,
                write=self._timeout_seconds,
                pool=self._timeout_seconds,
            )
            try:
                with httpx.Client(timeout=timeout) as client:
                    with client.stream(
                        "POST",
                        f"{self._base_url}/v1/synthesize/stream",
                        json=payload,
                    ) as response:
                        if response.status_code == 404:
                            # Backward-compatible fallback for older TTS service builds.
                            fallback = self.synthesize(
                                text=normalized_text,
                                voice=selected_voice,
                                language=selected_language,
                                instruct=selected_instruct,
                            )
                            if fallback.error:
                                yield self._sse_event("error", {"error": fallback.error})
                                yield self._sse_event("done", {"ok": False})
                                return
                            yield self._sse_event("start", {"chunks": 1, "fallback": True})
                            yield self._sse_event(
                                "chunk",
                                {
                                    "index": 1,
                                    "chunks": 1,
                                    "text": normalized_text,
                                    "audio_base64": fallback.audio_base64,
                                    "mime_type": fallback.mime_type or "audio/wav",
                                    "provider": fallback.provider or "tts-service",
                                },
                            )
                            yield self._sse_event("done", {"ok": True, "chunks": 1, "fallback": True})
                            return

                        if response.status_code >= 400:
                            detail = self._extract_error_detail(response)
                            logger.warning(
                                "TTS stream request failed with status=%s detail=%s",
                                response.status_code,
                                detail,
                            )
                            yield self._sse_event("error", {"error": detail})
                            yield self._sse_event("done", {"ok": False})
                            return

                        for chunk in response.iter_bytes():
                            if chunk:
                                yield chunk
            except Exception as exc:  # noqa: BLE001
                logger.warning("TTS stream unexpected failure: %s", exc)
                yield self._sse_event("error", {"error": str(exc)})
                yield self._sse_event("done", {"ok": False})

        return _iter()
