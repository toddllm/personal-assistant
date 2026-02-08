from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import sys
from threading import Lock
from typing import Any

import numpy as np

from tts_service.config import Settings

logger = logging.getLogger(__name__)


DEFAULT_QWEN_CUSTOM_VOICES = [
    "Vivian",
    "Serena",
    "Uncle_Fu",
    "Dylan",
    "Eric",
    "Ryan",
    "Aiden",
    "Ono_Anna",
    "Sohee",
]


@dataclass(slots=True)
class TTSResult:
    wav: np.ndarray
    sample_rate: int
    voice: str
    language: str
    provider: str


class Qwen3CustomVoiceEngine:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._model: Any | None = None
        self._model_lock = Lock()
        self._load_error: str | None = None
        self._supported_voices: list[str] = list(DEFAULT_QWEN_CUSTOM_VOICES)

    def status(self) -> dict[str, object]:
        return {
            "provider": "qwen3_custom_voice",
            "model_loaded": self._model is not None,
            "load_error": self._load_error,
        }

    def list_voices(self) -> list[str]:
        return list(self._supported_voices)

    def load(self) -> None:
        with self._model_lock:
            if self._model is not None:
                return

            try:
                import torch
            except Exception as exc:  # noqa: BLE001
                self._load_error = (
                    "PyTorch is unavailable in this environment. Install torch on toddllm "
                    f"before loading Qwen3-TTS: {exc}"
                )
                raise RuntimeError(self._load_error) from exc

            try:
                from qwen_tts import Qwen3TTSModel
            except Exception as exc:  # noqa: BLE001
                import_path = self._settings.qwen_import_path
                if import_path:
                    candidate = Path(import_path).expanduser().resolve()
                    if str(candidate) not in sys.path:
                        sys.path.insert(0, str(candidate))
                    try:
                        from qwen_tts import Qwen3TTSModel  # type: ignore[no-redef]
                    except Exception as path_exc:  # noqa: BLE001
                        self._load_error = (
                            "qwen-tts import failed even after adding "
                            f"TTS_SERVICE_QWEN_IMPORT_PATH={candidate!s}: {path_exc}"
                        )
                        raise RuntimeError(self._load_error) from path_exc
                else:
                    self._load_error = (
                        "qwen-tts is unavailable. Install optional dependencies with "
                        "`pip install -e .[tts]` or set TTS_SERVICE_QWEN_IMPORT_PATH "
                        "to an existing Qwen3-TTS checkout."
                    )
                    raise RuntimeError(self._load_error) from exc

            dtype_map = {
                "bfloat16": torch.bfloat16,
                "bf16": torch.bfloat16,
                "float16": torch.float16,
                "fp16": torch.float16,
                "float32": torch.float32,
                "fp32": torch.float32,
            }
            dtype = dtype_map.get(self._settings.qwen_dtype.lower())
            if dtype is None:
                raise RuntimeError(
                    f"Unsupported TTS_SERVICE_QWEN_DTYPE='{self._settings.qwen_dtype}'."
                )

            attn = self._settings.qwen_attn_implementation
            if attn is not None and attn.strip().lower() in {"", "none", "null"}:
                attn = None

            logger.info(
                "Loading qwen-tts model=%s device=%s dtype=%s attn=%s",
                self._settings.qwen_model_id,
                self._settings.qwen_device_map,
                self._settings.qwen_dtype,
                attn,
            )
            self._model = Qwen3TTSModel.from_pretrained(
                self._settings.qwen_model_id,
                device_map=self._settings.qwen_device_map,
                dtype=dtype,
                attn_implementation=attn,
            )
            supported = self._model.get_supported_speakers()
            if supported:
                self._supported_voices = sorted({str(v) for v in supported})
            self._load_error = None
            logger.info("Qwen3 TTS model loaded.")

    def synthesize(
        self,
        text: str,
        voice: str | None = None,
        language: str | None = None,
        instruct: str | None = None,
        max_new_tokens: int | None = None,
        temperature: float | None = None,
    ) -> TTSResult:
        if len(text) > self._settings.max_text_chars:
            raise RuntimeError(
                f"Text length {len(text)} exceeds limit {self._settings.max_text_chars}."
            )

        if self._model is None:
            self.load()
        if self._model is None:
            raise RuntimeError(self._load_error or "Qwen3 TTS model is unavailable.")

        selected_voice = voice or self._settings.default_voice
        selected_language = language or self._settings.default_language
        selected_instruct = instruct if instruct is not None else self._settings.default_instruct

        kwargs: dict[str, object] = {}
        kwargs["max_new_tokens"] = max_new_tokens or self._settings.max_new_tokens
        kwargs["temperature"] = (
            float(temperature) if temperature is not None else self._settings.temperature
        )

        wavs, sample_rate = self._model.generate_custom_voice(
            text=text.strip(),
            speaker=selected_voice,
            language=selected_language,
            instruct=selected_instruct,
            **kwargs,
        )
        if not wavs:
            raise RuntimeError("TTS engine returned no audio.")
        wav = np.asarray(wavs[0], dtype=np.float32)
        return TTSResult(
            wav=wav,
            sample_rate=int(sample_rate),
            voice=selected_voice,
            language=selected_language,
            provider="qwen3_custom_voice",
        )


class StubTTSEngine:
    def __init__(self, settings: Settings):
        self._settings = settings

    def status(self) -> dict[str, object]:
        return {
            "provider": "stub",
            "model_loaded": True,
            "load_error": None,
        }

    def list_voices(self) -> list[str]:
        return ["stub-tone"]

    def load(self) -> None:
        return

    def synthesize(
        self,
        text: str,
        voice: str | None = None,
        language: str | None = None,
        instruct: str | None = None,
        max_new_tokens: int | None = None,
        temperature: float | None = None,
    ) -> TTSResult:
        del max_new_tokens, instruct, temperature
        sample_rate = 24_000
        duration = min(max(1.0, len(text) / 60.0), 6.0)
        t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
        wav = (0.18 * np.sin(2 * np.pi * 330.0 * t)).astype(np.float32)
        return TTSResult(
            wav=wav,
            sample_rate=sample_rate,
            voice=voice or "stub-tone",
            language=language or "English",
            provider="stub",
        )
