#!/usr/bin/env python3
"""Multi-mic forwarding daemon: all physical mics -> CaptureMic 2ch virtual device.

Architecture:
  - One input reader thread per configured mic (reads, applies gain,
    resamples, upmixes to stereo, queues frames)
  - One watchdog thread per mic (subprocess device check every 2s)
  - One mixer thread (reads all queues, sums, clips [-1,1], writes to
    CaptureMic 2ch)
  - One settings poller thread (reads data/mic-settings.json every 1s,
    updates per-mic volume & enabled flags)

This mirrors the audio-forward.py pattern (one thread per device,
per-device queues, subprocess watchdog, PortAudio reinit protocol)
but in the opposite direction: many inputs -> one output.

Configuration via AUDIO_ASSIST_* environment variables:

    AUDIO_ASSIST_MIC_FORWARD_ENABLED=true
    AUDIO_ASSIST_MIC_FORWARD_SOURCE_DEVICES="Bose QC45,MacBook Pro Microphone"
    AUDIO_ASSIST_MIC_FORWARD_TARGET_DEVICE="CaptureMic 2ch"
"""

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from queue import Empty, Full, Queue

import numpy as np
import sounddevice as sd

# ---------------------------------------------------------------------------
# Bootstrap: add project root to sys.path so we can import config
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "src"))

from audio_assist.config import settings  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mic-forward")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TARGET_DEVICE = settings.mic_forward_target_device
SOURCE_DEVICES = [s.strip() for s in settings.mic_forward_source_devices.split(",") if s.strip()]
TARGET_RATE = settings.mic_forward_sample_rate
TARGET_CHANNELS = settings.mic_forward_channels
BLOCKSIZE = settings.mic_forward_blocksize
AUTO_DISCOVER = settings.mic_forward_auto_discover
EXCLUDED_SUBSTRINGS = [s.strip().lower() for s in settings.audio_hub_excluded_devices.split(",") if s.strip()]
DEVICE_CHECK_INTERVAL = 2.0
RETRY_INTERVAL = 5.0
DISCOVERY_INTERVAL = 5.0
SETTINGS_POLL_INTERVAL = 1.0
LEVELS_WRITE_INTERVAL = 0.2  # write level meters every 200ms
MIC_SETTINGS_PATH = os.path.join(_PROJECT_ROOT, "data", "mic-settings.json")
MIC_LEVELS_PATH = os.path.join(_PROJECT_ROOT, "data", "mic-levels.json")

_shutdown = threading.Event()


# ---------------------------------------------------------------------------
# Per-mic state
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    return name.lower().replace(" ", "-")


PROTECTED_MIC_SLUGS: set[str] = set()


def _normalize_mic_setting(slug: str, data: dict | None = None) -> dict:
    normalized = dict(data or {})
    if slug in PROTECTED_MIC_SLUGS:
        current_volume = normalized.get("volume", 0)
        if isinstance(current_volume, (int, float)) and current_volume > 0:
            normalized.setdefault("pre_disable_gain", int(current_volume))
        normalized["volume"] = 0
        normalized["enabled"] = False
        return normalized
    normalized["volume"] = int(normalized.get("volume", 100))
    normalized["enabled"] = bool(normalized.get("enabled", True))
    return normalized


def _default_mic_setting(slug: str) -> dict:
    return _normalize_mic_setting(slug, {"volume": 100, "enabled": True})


@dataclass
class MicState:
    name: str
    slug: str
    volume: int = 100      # 0-200 digital gain (>100 = boost)
    enabled: bool = True
    queue: Queue = field(default_factory=lambda: Queue(maxsize=24))
    level_dbfs: float = -100.0   # RMS level after gain
    peak_dbfs: float = -100.0    # peak level after gain


# Global mic states, keyed by slug
_mic_states: dict[str, MicState] = {}
_mic_states_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Settings poller
# ---------------------------------------------------------------------------

def _load_mic_settings() -> dict[str, dict]:
    """Read mic settings from JSON file."""
    try:
        with open(MIC_SETTINGS_PATH) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {slug: _normalize_mic_setting(slug, data) for slug, data in raw.items()}


def _settings_poller() -> None:
    """Poll data/mic-settings.json every 1s and update in-memory mic states."""
    while not _shutdown.is_set():
        _shutdown.wait(SETTINGS_POLL_INTERVAL)
        if _shutdown.is_set():
            break
        try:
            file_settings = _load_mic_settings()
            with _mic_states_lock:
                for slug, state in _mic_states.items():
                    if slug in file_settings:
                        s = file_settings[slug]
                        state.volume = s.get("volume", 100)
                        state.enabled = s.get("enabled", True)
        except Exception:
            log.debug("settings poll error", exc_info=True)

    log.info("settings poller stopped")


def _levels_writer() -> None:
    """Write per-mic dBFS levels to data/mic-levels.json every 200ms."""
    while not _shutdown.is_set():
        _shutdown.wait(LEVELS_WRITE_INTERVAL)
        if _shutdown.is_set():
            break
        try:
            with _mic_states_lock:
                levels = {
                    slug: {
                        "level_dbfs": round(st.level_dbfs, 1),
                        "peak_dbfs": round(st.peak_dbfs, 1),
                        "volume": st.volume,
                        "enabled": st.enabled,
                    }
                    for slug, st in _mic_states.items()
                }
            fd, tmp = tempfile.mkstemp(
                dir=os.path.dirname(MIC_LEVELS_PATH), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(levels, f)
                    f.write("\n")
                os.replace(tmp, MIC_LEVELS_PATH)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        except Exception:
            log.debug("levels write error", exc_info=True)

    log.info("levels writer stopped")


def _save_mic_settings(data: dict[str, dict]) -> None:
    """Atomically write mic settings to JSON file."""
    os.makedirs(os.path.dirname(MIC_SETTINGS_PATH), exist_ok=True)
    normalized = {slug: _normalize_mic_setting(slug, value) for slug, value in data.items()}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(MIC_SETTINGS_PATH), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(normalized, f, indent=2)
            f.write("\n")
        os.replace(tmp, MIC_SETTINGS_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------

# Track input reader threads by slug so discovery can start new ones
_input_threads: dict[str, threading.Thread] = {}
_input_threads_lock = threading.Lock()


def _is_excluded(name: str) -> bool:
    """Check if a device name matches any excluded substring."""
    name_lower = name.lower()
    return any(ex in name_lower for ex in EXCLUDED_SUBSTRINGS)


def _discover_input_devices_subprocess() -> list[str]:
    """Discover all physical input devices via a subprocess (fresh PortAudio scan)."""
    script = (
        "import json, sounddevice as sd; "
        "devs = sd.query_devices(); "
        "print(json.dumps([d['name'] for d in devs if d['max_input_channels'] > 0]))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=5,
        )
        names = json.loads(result.stdout.strip())
        return [n for n in names if not _is_excluded(n)]
    except Exception:
        log.debug("discovery subprocess error", exc_info=True)
        return []


def _discovery_thread() -> None:
    """Periodically scan for new input devices and start reader threads."""
    while not _shutdown.is_set():
        _shutdown.wait(DISCOVERY_INTERVAL)
        if _shutdown.is_set():
            break
        try:
            discovered = _discover_input_devices_subprocess()
            for dev_name in discovered:
                slug = _slugify(dev_name)
                with _mic_states_lock:
                    if slug in _mic_states:
                        continue  # already known
                with _input_threads_lock:
                    if slug in _input_threads and _input_threads[slug].is_alive():
                        continue  # thread already running

                # New device found
                log.info("discovered new input: %s [%s]", dev_name, slug)
                state = MicState(name=dev_name, slug=slug)
                with _mic_states_lock:
                    _mic_states[slug] = state

                # Auto-populate settings file
                file_settings = _load_mic_settings()
                if slug not in file_settings:
                    file_settings[slug] = _default_mic_setting(slug)
                    _save_mic_settings(file_settings)
                else:
                    state.volume = file_settings[slug]["volume"]
                    state.enabled = file_settings[slug]["enabled"]

                # Start input reader thread
                t = threading.Thread(
                    target=_input_reader,
                    args=(dev_name, slug),
                    name=f"in-{slug}",
                    daemon=True,
                )
                t.start()
                with _input_threads_lock:
                    _input_threads[slug] = t
                log.info("started input reader for discovered '%s' [%s]", dev_name, slug)
        except Exception:
            log.debug("discovery thread error", exc_info=True)

    log.info("discovery thread stopped")


# ---------------------------------------------------------------------------
# Device discovery
# ---------------------------------------------------------------------------

def find_device(name: str, kind: str | None = None) -> int | None:
    """Find device index by name substring (case-insensitive)."""
    for i, dev in enumerate(sd.query_devices()):
        if name.lower() in dev["name"].lower():
            if kind == "input" and dev["max_input_channels"] < 1:
                continue
            if kind == "output" and dev["max_output_channels"] < 1:
                continue
            return i
    return None


def check_device_exists_subprocess(name: str, kind: str) -> bool:
    """Check if a device exists using a fresh PortAudio instance."""
    script = (
        "import sounddevice as sd; "
        f"devs = sd.query_devices(); "
        f"print(any('{name}'.lower() in d['name'].lower() "
        f"and d['max_{kind}_channels'] > 0 for d in devs))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() == "True"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Audio processing
# ---------------------------------------------------------------------------

def resample(data: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Resample audio data using linear interpolation."""
    if from_rate == to_rate:
        return data
    ratio = to_rate / from_rate
    n_out = max(1, int(len(data) * ratio))
    indices = np.linspace(0, len(data) - 1, n_out)
    if data.ndim == 1:
        return np.interp(indices, np.arange(len(data)), data).astype(data.dtype)
    result = np.empty((n_out, data.shape[1]), dtype=data.dtype)
    for ch in range(data.shape[1]):
        result[:, ch] = np.interp(indices, np.arange(len(data)), data[:, ch])
    return result


def upmix_to_stereo(data: np.ndarray) -> np.ndarray:
    """Duplicate mono to stereo. Returns (n_samples, 2)."""
    if data.ndim == 1:
        data = data.reshape(-1, 1)
    if data.shape[1] >= 2:
        return data
    return np.column_stack([data[:, 0], data[:, 0]])


# ---------------------------------------------------------------------------
# Refresh PortAudio
# ---------------------------------------------------------------------------

_reinit_needed = threading.Event()


def _refresh_portaudio() -> None:
    """Terminate and reinitialize PortAudio to pick up new devices."""
    log.info("refreshing PortAudio device list")
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        log.debug("PortAudio refresh error (non-fatal)", exc_info=True)


# ---------------------------------------------------------------------------
# Per-mic input reader thread
# ---------------------------------------------------------------------------

def _input_reader(mic_name: str, slug: str) -> None:
    """Read from one physical mic, apply gain/resample/upmix, queue frames."""
    while not _shutdown.is_set():
        # Check if enabled
        with _mic_states_lock:
            state = _mic_states.get(slug)
        if state is None:
            break
        if not state.enabled:
            _shutdown.wait(0.5)
            continue

        # Handle reinit
        if _reinit_needed.is_set():
            _shutdown.wait(1.0)
            continue

        # Find device
        mic_idx = find_device(mic_name, kind="input")
        if mic_idx is None:
            # Check via subprocess — may need reinit
            if check_device_exists_subprocess(mic_name, "input"):
                log.info("[%s] found via subprocess, requesting reinit", slug)
                _reinit_needed.set()
                _shutdown.wait(2.0)
                continue
            log.info("[%s] not connected, retrying in %.0fs", slug, RETRY_INTERVAL)
            _shutdown.wait(RETRY_INTERVAL)
            continue

        mic_info = sd.query_devices(mic_idx)
        mic_rate = int(mic_info["default_samplerate"])
        mic_channels = mic_info["max_input_channels"]
        needs_resample = mic_rate != TARGET_RATE
        needs_upmix = mic_channels < TARGET_CHANNELS

        log.info("[%s] opening: idx %d, %dch @ %d Hz%s%s",
                 slug, mic_idx, mic_channels, mic_rate,
                 " [resample]" if needs_resample else "",
                 " [upmix]" if needs_upmix else "")

        # Watchdog: detect mic disconnect
        device_gone = threading.Event()

        def _watchdog(name=mic_name, s=slug, gone=device_gone):
            while not _shutdown.is_set() and not gone.is_set():
                _shutdown.wait(DEVICE_CHECK_INTERVAL)
                if gone.is_set() or _shutdown.is_set():
                    break
                if not check_device_exists_subprocess(name, "input"):
                    log.warning("[%s] watchdog: mic disappeared", s)
                    gone.set()

        wd = threading.Thread(target=_watchdog, name=f"wd-{slug}", daemon=True)
        wd.start()

        try:
            with sd.InputStream(
                device=mic_idx,
                samplerate=mic_rate,
                channels=mic_channels,
                blocksize=BLOCKSIZE,
                dtype="float32",
            ) as in_stream:
                log.info("[%s] active", slug)

                while (not _shutdown.is_set()
                       and not device_gone.is_set()
                       and not _reinit_needed.is_set()):
                    # Check enabled/volume each iteration
                    with _mic_states_lock:
                        st = _mic_states.get(slug)
                    if st is None or not st.enabled:
                        break

                    data, overflowed = in_stream.read(BLOCKSIZE)
                    if overflowed:
                        log.debug("[%s] input overflow", slug)

                    # Apply digital gain
                    gain = st.volume / 100.0
                    if gain != 1.0:
                        data = data * gain

                    # Compute levels after gain for live metering
                    rms = float(np.sqrt(np.mean(data ** 2)))
                    peak = float(np.max(np.abs(data)))
                    rms_db = 20.0 * np.log10(max(rms, 1e-10))
                    peak_db = 20.0 * np.log10(max(peak, 1e-10))
                    with _mic_states_lock:
                        st.level_dbfs = rms_db
                        st.peak_dbfs = peak_db

                    # Resample to target rate
                    if needs_resample:
                        data = resample(data, mic_rate, TARGET_RATE)

                    # Upmix mono -> stereo
                    if needs_upmix:
                        data = upmix_to_stereo(data)

                    # Queue for mixer — drop oldest to bound latency
                    try:
                        st.queue.put_nowait(data)
                    except Full:
                        try:
                            st.queue.get_nowait()
                        except Empty:
                            pass
                        try:
                            st.queue.put_nowait(data)
                        except Full:
                            pass

        except sd.PortAudioError as exc:
            if _shutdown.is_set():
                return
            if check_device_exists_subprocess(mic_name, "input"):
                log.info("[%s] PortAudio error but mic exists — requesting reinit", slug)
                _reinit_needed.set()
            else:
                log.error("[%s] PortAudio error: %s", slug, exc)
        except Exception:
            if _shutdown.is_set():
                return
            log.exception("[%s] unexpected error", slug)
        finally:
            device_gone.set()  # stop watchdog

        if not _shutdown.is_set():
            if _reinit_needed.is_set():
                _shutdown.wait(1.0)
            else:
                _shutdown.wait(RETRY_INTERVAL)

    log.info("[%s] input reader stopped", slug)


# ---------------------------------------------------------------------------
# Mixer/writer thread
# ---------------------------------------------------------------------------

def _mixer_writer(target_name: str) -> None:
    """Sum all mic queues and write to the output device."""
    # Expected frame shape after resample/upmix
    expected_samples = BLOCKSIZE  # approximate — resampled blocks may vary slightly

    while not _shutdown.is_set():
        # Handle reinit
        if _reinit_needed.is_set():
            _refresh_portaudio()
            _reinit_needed.clear()

        # Find target device
        target_idx = find_device(target_name, kind="output")
        if target_idx is None:
            log.warning("target '%s' not found, retrying in %.0fs",
                        target_name, RETRY_INTERVAL)
            _shutdown.wait(RETRY_INTERVAL)
            continue

        log.info("mixer: opening %s (idx %d, %dch @ %d Hz)",
                 target_name, target_idx, TARGET_CHANNELS, TARGET_RATE)

        try:
            with sd.OutputStream(
                device=target_idx,
                samplerate=TARGET_RATE,
                channels=TARGET_CHANNELS,
                dtype="float32",
                latency="low",
            ) as out_stream:
                log.info("mixer active -> %s", target_name)

                while not _shutdown.is_set() and not _reinit_needed.is_set():
                    frames = []
                    with _mic_states_lock:
                        states = list(_mic_states.values())

                    for st in states:
                        if not st.enabled:
                            # Drain queue when disabled
                            while not st.queue.empty():
                                try:
                                    st.queue.get_nowait()
                                except Empty:
                                    break
                            continue
                        try:
                            data = st.queue.get_nowait()
                            frames.append(data)
                        except Empty:
                            pass

                    if not frames:
                        # No data from any mic — sleep briefly to avoid busy-wait
                        time.sleep(0.002)
                        continue

                    # Sum all frames (they should all be TARGET_CHANNELS-wide float32)
                    # Handle slightly different lengths from resampling
                    min_len = min(f.shape[0] for f in frames)
                    mixed = np.zeros((min_len, TARGET_CHANNELS), dtype=np.float32)
                    for f in frames:
                        mixed += f[:min_len]

                    # Hard clip to [-1, 1]
                    np.clip(mixed, -1.0, 1.0, out=mixed)

                    try:
                        out_stream.write(mixed)
                    except sd.PortAudioError:
                        log.warning("mixer: write error")
                        break

        except sd.PortAudioError as exc:
            if _shutdown.is_set():
                return
            log.error("mixer PortAudio error: %s — retrying in %.0fs",
                      exc, RETRY_INTERVAL)
        except Exception:
            if _shutdown.is_set():
                return
            log.exception("mixer error — retrying in %.0fs", RETRY_INTERVAL)

        if not _shutdown.is_set():
            if _reinit_needed.is_set():
                _shutdown.wait(1.0)
            else:
                _shutdown.wait(RETRY_INTERVAL)

    log.info("mixer stopped")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not settings.mic_forward_enabled:
        log.info("mic forwarding disabled")
        return

    if not SOURCE_DEVICES:
        log.error("no source devices configured")
        return

    log.info("mic-forward starting (multi-mic mixer)")
    log.info("  target: %s (%dch @ %d Hz)", TARGET_DEVICE, TARGET_CHANNELS, TARGET_RATE)
    log.info("  seed mics: %s", SOURCE_DEVICES)
    log.info("  auto-discover: %s", AUTO_DISCOVER)

    # Initialize per-mic states from seed list
    initial_devices = list(SOURCE_DEVICES)

    # If auto-discover, also scan for devices now
    if AUTO_DISCOVER:
        discovered = _discover_input_devices_subprocess()
        for dev_name in discovered:
            if dev_name not in initial_devices:
                initial_devices.append(dev_name)
                log.info("  discovered: %s", dev_name)

    for mic_name in initial_devices:
        slug = _slugify(mic_name)
        _mic_states[slug] = MicState(name=mic_name, slug=slug)

    # Load initial settings from file
    file_settings = _load_mic_settings()
    settings_changed = False
    for slug, state in _mic_states.items():
        if slug in file_settings:
            s = file_settings[slug]
            state.volume = s["volume"]
            state.enabled = s["enabled"]
            log.info("  [%s] volume=%d enabled=%s", slug, state.volume, state.enabled)
        else:
            log.info("  [%s] volume=%d enabled=%s (default)", slug, state.volume, state.enabled)
            file_settings[slug] = _default_mic_setting(slug)
            settings_changed = True
    if settings_changed:
        _save_mic_settings(file_settings)

    def _handle_signal(signum, frame):
        sig = signal.Signals(signum).name
        log.info("received %s, shutting down", sig)
        _shutdown.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    threads: list[threading.Thread] = []

    # Settings poller thread
    t = threading.Thread(target=_settings_poller, name="settings-poller", daemon=True)
    t.start()
    threads.append(t)
    log.info("started settings poller")

    # Levels writer thread
    t = threading.Thread(target=_levels_writer, name="levels-writer", daemon=True)
    t.start()
    threads.append(t)
    log.info("started levels writer -> %s", MIC_LEVELS_PATH)

    # One input reader thread per mic
    for slug, state in _mic_states.items():
        t = threading.Thread(
            target=_input_reader,
            args=(state.name, slug),
            name=f"in-{slug}",
            daemon=True,
        )
        t.start()
        threads.append(t)
        with _input_threads_lock:
            _input_threads[slug] = t
        log.info("started input reader for '%s' [%s]", state.name, slug)

    # Discovery thread (if auto-discover enabled)
    if AUTO_DISCOVER:
        t = threading.Thread(target=_discovery_thread, name="discovery", daemon=True)
        t.start()
        threads.append(t)
        log.info("started discovery thread (interval=%.0fs)", DISCOVERY_INTERVAL)

    # Mixer/writer thread
    t = threading.Thread(
        target=_mixer_writer,
        args=(TARGET_DEVICE,),
        name="mixer",
        daemon=True,
    )
    t.start()
    threads.append(t)
    log.info("started mixer -> '%s'", TARGET_DEVICE)

    # Wait for shutdown
    _shutdown.wait()

    # Wait for threads to finish
    for t in threads:
        t.join(timeout=3.0)

    log.info("mic-forward exiting")


if __name__ == "__main__":
    main()
