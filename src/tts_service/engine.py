from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
import sys
from threading import Lock
from typing import Any

import numpy as np

from tts_service.config import Settings

logger = logging.getLogger(__name__)


class VoiceProfileStore:
    """Manages cloned voice profiles persisted to a JSON file."""

    def __init__(self, profiles_dir: Path) -> None:
        self._dir = profiles_dir
        self._audio_dir = profiles_dir / "audio"
        self._photos_dir = profiles_dir / "photos"
        self._json_path = profiles_dir / "profiles.json"
        self._profiles: dict[str, dict[str, Any]] = {}
        self._deleted: set[str] = set()
        self._ensure_dirs()
        self._load()

    def _ensure_dirs(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._photos_dir.mkdir(parents=True, exist_ok=True)

    def _load(self) -> None:
        if self._json_path.exists():
            try:
                data = json.loads(self._json_path.read_text())
                # Support both old format (flat dict) and new format with _deleted key
                if "_deleted" in data and isinstance(data["_deleted"], list):
                    self._deleted = set(data.pop("_deleted"))
                self._profiles = data
            except Exception:
                logger.exception("Failed to load voice profiles from %s", self._json_path)
                self._profiles = {}

    def _save(self) -> None:
        data = dict(self._profiles)
        if self._deleted:
            data["_deleted"] = sorted(self._deleted)
        self._json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def list_profiles(self) -> dict[str, dict[str, Any]]:
        return dict(self._profiles)

    def get(self, name: str) -> dict[str, Any] | None:
        return self._profiles.get(name)

    def add(
        self,
        name: str,
        display_name: str,
        ref_audio_path: str,
        ref_text: str,
        *,
        is_owner: bool = False,
        x_vector_only: bool = False,
    ) -> dict[str, Any]:
        profile = {
            "display_name": display_name,
            "ref_audio_path": ref_audio_path,
            "ref_text": ref_text,
            "x_vector_only": x_vector_only,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "is_owner": is_owner,
            "photo_path": None,
        }
        self._profiles[name] = profile
        self._save()
        return profile

    def update(self, name: str, **kwargs: Any) -> dict[str, Any] | None:
        profile = self._profiles.get(name)
        if profile is None:
            return None
        for key, value in kwargs.items():
            if key in profile:
                profile[key] = value
        self._save()
        return profile

    def remove(self, name: str) -> bool:
        profile = self._profiles.pop(name, None)
        if profile is None:
            return False
        self._deleted.add(name)
        # Clean up audio file
        audio_path = Path(profile.get("ref_audio_path", ""))
        if audio_path.exists():
            audio_path.unlink(missing_ok=True)
        # Clean up photo file
        photo_path = profile.get("photo_path")
        if photo_path:
            Path(photo_path).unlink(missing_ok=True)
        self._save()
        return True

    def is_deleted(self, name: str) -> bool:
        """Check if a voice name was previously deleted (to prevent re-import)."""
        return name in self._deleted

    def set_photo(self, name: str, photo_bytes: bytes, ext: str = ".jpg") -> str | None:
        profile = self._profiles.get(name)
        if profile is None:
            return None
        photo_path = self._photos_dir / f"{name}{ext}"
        photo_path.write_bytes(photo_bytes)
        profile["photo_path"] = str(photo_path)
        self._save()
        return str(photo_path)

    def get_photo_path(self, name: str) -> str | None:
        profile = self._profiles.get(name)
        if profile is None:
            return None
        return profile.get("photo_path")

    @property
    def audio_dir(self) -> Path:
        return self._audio_dir


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


QWEN_BASE_MODEL_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"


class Qwen3CustomVoiceEngine:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._model: Any | None = None
        self._clone_model: Any | None = None  # Base model for voice cloning
        self._model_lock = Lock()
        self._load_error: str | None = None
        self._supported_voices: list[str] = list(DEFAULT_QWEN_CUSTOM_VOICES)
        self._profile_store = VoiceProfileStore(settings.voice_profiles_dir)
        self._cached_prompts: dict[str, Any] = {}

    def status(self) -> dict[str, object]:
        return {
            "provider": "qwen3_custom_voice",
            "model_loaded": self._model is not None,
            "load_error": self._load_error,
        }

    @property
    def profile_store(self) -> VoiceProfileStore:
        return self._profile_store

    def list_voices(self) -> list[str]:
        builtin = list(self._supported_voices)
        cloned = list(self._profile_store.list_profiles().keys())
        return builtin + [n for n in cloned if n not in builtin]

    def list_voices_detailed(self) -> list[dict[str, Any]]:
        """Return voice info with type indicators."""
        result: list[dict[str, Any]] = []
        for name in self._supported_voices:
            result.append({"name": name, "display_name": name, "voice_type": "builtin",
                           "is_owner": False, "has_photo": False, "created_at": None})
        for name, profile in self._profile_store.list_profiles().items():
            result.append({
                "name": name,
                "display_name": profile.get("display_name", name),
                "voice_type": "cloned",
                "is_owner": profile.get("is_owner", False),
                "has_photo": profile.get("photo_path") is not None,
                "created_at": profile.get("created_at"),
            })
        return result

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
            logger.info("Qwen3 TTS CustomVoice model loaded.")

            # Load the Base model for voice cloning (separate model variant)
            logger.info("Loading qwen-tts Base model for voice cloning...")
            try:
                self._clone_model = Qwen3TTSModel.from_pretrained(
                    QWEN_BASE_MODEL_ID,
                    device_map=self._settings.qwen_device_map,
                    dtype=dtype,
                    attn_implementation=attn,
                )
                logger.info("Qwen3 TTS Base (clone) model loaded.")
            except Exception:
                logger.exception(
                    "Failed to load Base model for voice cloning. "
                    "Cloned voices will not work, but builtin voices are fine."
                )

            self._migrate_legacy_clones()
            self._precompute_clone_prompts()

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

        profile = self._profile_store.get(selected_voice)
        if profile:
            if self._clone_model is None:
                raise RuntimeError(
                    "Voice cloning unavailable: Base model not loaded. "
                    "Restart service to retry loading."
                )
            cached = self._cached_prompts.get(selected_voice)
            if cached is not None:
                wavs, sample_rate = self._clone_model.generate_voice_clone(
                    text=text.strip(),
                    language=selected_language,
                    voice_clone_prompt=cached,
                    **kwargs,
                )
            else:
                wavs, sample_rate = self._clone_model.generate_voice_clone(
                    text=text.strip(),
                    language=selected_language,
                    ref_audio=profile["ref_audio_path"],
                    ref_text=profile["ref_text"],
                    x_vector_only_mode=profile.get("x_vector_only", False),
                    **kwargs,
                )
            provider = "qwen3_voice_clone"
        else:
            wavs, sample_rate = self._model.generate_custom_voice(
                text=text.strip(),
                speaker=selected_voice,
                language=selected_language,
                instruct=selected_instruct,
                **kwargs,
            )
            provider = "qwen3_custom_voice"

        if not wavs:
            raise RuntimeError("TTS engine returned no audio.")
        wav = np.asarray(wavs[0], dtype=np.float32)
        return TTSResult(
            wav=wav,
            sample_rate=int(sample_rate),
            voice=selected_voice,
            language=selected_language,
            provider=provider,
        )


    def clone_voice(
        self,
        name: str,
        ref_audio_bytes: bytes,
        ref_text: str,
        display_name: str | None = None,
        is_owner: bool = False,
        x_vector_only: bool = False,
    ) -> dict[str, Any]:
        """Clone a voice from reference audio and register it."""
        if self._profile_store.get(name) is not None:
            raise RuntimeError(f"Voice profile '{name}' already exists.")
        if name in self._supported_voices:
            raise RuntimeError(f"'{name}' conflicts with a builtin voice name.")

        audio_path = self._profile_store.audio_dir / f"{name}_ref.wav"
        audio_path.write_bytes(ref_audio_bytes)

        profile = self._profile_store.add(
            name=name,
            display_name=display_name or name,
            ref_audio_path=str(audio_path),
            ref_text=ref_text,
            is_owner=is_owner,
            x_vector_only=x_vector_only,
        )

        # Pre-compute clone prompt items if Base model is loaded
        if self._clone_model is not None:
            try:
                prompt = self._clone_model.create_voice_clone_prompt(
                    ref_audio=str(audio_path),
                    ref_text=ref_text,
                    x_vector_only_mode=x_vector_only,
                )
                self._cached_prompts[name] = prompt
                logger.info("Pre-computed clone prompt for '%s'", name)
            except Exception:
                logger.exception("Failed to pre-compute clone prompt for '%s'", name)

        return profile

    def remove_voice(self, name: str) -> bool:
        """Remove a cloned voice profile."""
        if name in self._supported_voices:
            raise RuntimeError(f"Cannot remove builtin voice '{name}'.")
        self._cached_prompts.pop(name, None)
        return self._profile_store.remove(name)

    def _migrate_legacy_clones(self) -> None:
        """Import voices from legacy Qwen3-TTS cloned_voices.json if configured."""
        legacy_json = self._settings.legacy_clones_json
        if not legacy_json:
            return
        legacy_path = Path(legacy_json)
        if not legacy_path.exists():
            logger.warning("Legacy clones JSON not found: %s", legacy_path)
            return

        legacy_audio_dir = Path(self._settings.legacy_clones_audio_dir or legacy_path.parent / "cloned_audio")

        try:
            legacy_data = json.loads(legacy_path.read_text())
        except Exception:
            logger.exception("Failed to read legacy clones from %s", legacy_path)
            return

        for voice_name, voice_info in legacy_data.items():
            # Normalize name to lowercase slug (legacy uses display names as keys)
            slug = voice_name.lower().replace(" ", "_")
            if self._profile_store.get(slug) is not None:
                continue  # Already migrated
            if self._profile_store.is_deleted(slug):
                continue  # User deleted this voice, don't re-import
            # Legacy format uses "audio_file" (filename only) or "ref_audio" (path)
            ref_audio = voice_info.get("audio_file") or voice_info.get("ref_audio", "")
            ref_text = voice_info.get("ref_text", "")
            if not ref_audio or not ref_text:
                logger.warning("Skipping legacy clone '%s': missing audio_file/ref_audio or ref_text", voice_name)
                continue

            src_audio = Path(ref_audio)
            if not src_audio.is_absolute():
                src_audio = legacy_audio_dir / ref_audio

            if not src_audio.exists():
                logger.warning("Skipping legacy clone '%s': audio not found at %s", voice_name, src_audio)
                continue

            dest_audio = self._profile_store.audio_dir / f"{slug}_ref.wav"
            shutil.copy2(src_audio, dest_audio)

            self._profile_store.add(
                name=slug,
                display_name=voice_info.get("display_name", voice_name),
                ref_audio_path=str(dest_audio),
                ref_text=ref_text,
                is_owner=voice_info.get("is_owner", False),
                x_vector_only=voice_info.get("x_vector_only", False),
            )
            logger.info("Migrated legacy clone: %s -> %s", voice_name, slug)

    def _precompute_clone_prompts(self) -> None:
        """Pre-compute voice clone prompts for all registered profiles."""
        if self._clone_model is None:
            logger.warning("Skipping clone prompt precomputation: Base model not loaded.")
            return
        for name, profile in self._profile_store.list_profiles().items():
            if name in self._cached_prompts:
                continue
            try:
                prompt = self._clone_model.create_voice_clone_prompt(
                    ref_audio=profile["ref_audio_path"],
                    ref_text=profile["ref_text"],
                    x_vector_only_mode=profile.get("x_vector_only", False),
                )
                self._cached_prompts[name] = prompt
                logger.info("Pre-computed clone prompt for '%s'", name)
            except Exception:
                logger.exception("Failed to pre-compute clone prompt for '%s'", name)


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
