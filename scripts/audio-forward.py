#!/usr/bin/env python3
"""Audio forwarding daemon: CaptureAudio 2ch -> real speakers/headphones.

Architecture:
  - Single InputStream reads CaptureAudio at 48 kHz stereo
  - Input callback distributes frames to per-target queues
  - Each target has its own OutputStream at its native rate/channels
  - Resampling (48 kHz -> 16 kHz) and channel downmix (stereo -> mono)
    happen in each output thread before writing
  - Targets that aren't connected are retried every 5 seconds
  - Targets that disconnect are detected via subprocess device check
    and torn down within 2 seconds

This avoids two problems with the full-duplex sd.Stream approach:
  1. Multiple readers at different sample rates cause noise
  2. Bluetooth HFP devices reject 48 kHz streams (need their native 16 kHz)

Configuration via AUDIO_ASSIST_* environment variables:

    AUDIO_ASSIST_AUDIO_FORWARD_TARGET_DEVICES="MacBook Pro Speakers,Bose QC45"
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
log = logging.getLogger("audio-forward")

# ---------------------------------------------------------------------------
# Auto-discovery config
# ---------------------------------------------------------------------------
AUTO_DISCOVER = settings.audio_forward_auto_discover
EXCLUDED_SUBSTRINGS = [s.strip().lower() for s in settings.audio_hub_excluded_devices.split(",") if s.strip()]
DISCOVERY_INTERVAL = 5.0
SPEAKER_SETTINGS_PATH = os.path.join(_PROJECT_ROOT, "data", "speaker-settings.json")

# Track output writer threads by device name
_output_threads: dict[str, threading.Thread] = {}
_output_threads_lock = threading.Lock()


def _is_excluded(name: str) -> bool:
    name_lower = name.lower()
    return any(ex in name_lower for ex in EXCLUDED_SUBSTRINGS)


def _load_speaker_settings() -> dict[str, dict]:
    try:
        with open(SPEAKER_SETTINGS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_speaker_settings(data: dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(SPEAKER_SETTINGS_PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(SPEAKER_SETTINGS_PATH), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, SPEAKER_SETTINGS_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _slugify(name: str) -> str:
    return name.lower().replace(" ", "-")


def _discover_output_devices_subprocess() -> list[str]:
    """Discover all physical output devices via a subprocess."""
    script = (
        "import json, sounddevice as sd; "
        "devs = sd.query_devices(); "
        "print(json.dumps([d['name'] for d in devs if d['max_output_channels'] > 0]))"
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


# ---------------------------------------------------------------------------
# Device discovery
# ---------------------------------------------------------------------------


def find_device(name: str, kind: str | None = None) -> int | None:
    """Find device index by name substring.

    *kind* can be ``"input"``, ``"output"``, or ``None`` (any).
    Returns None if no matching device is found (e.g. Bluetooth off).
    """
    for i, dev in enumerate(sd.query_devices()):
        if name.lower() in dev["name"].lower():
            if kind == "input" and dev["max_input_channels"] < 1:
                continue
            if kind == "output" and dev["max_output_channels"] < 1:
                continue
            return i
    return None


def check_device_exists_subprocess(name: str, kind: str) -> bool:
    """Check if a device exists using a fresh PortAudio instance.

    Spawns a subprocess so that PortAudio re-scans the device list
    without disrupting any active streams in this process.
    """
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
# Audio processing helpers
# ---------------------------------------------------------------------------


class StreamingResampler:
    """Lightweight streaming resampler using linear interpolation.

    Tracks fractional sample position across blocks to prevent sample
    count drift (which causes crackling over time). Zero internal
    buffering — no added latency.
    """

    def __init__(self, from_rate: int, to_rate: int):
        self.ratio = to_rate / from_rate
        self.frac = 0.0  # accumulated fractional sample carry-over

    def process(self, data: np.ndarray) -> np.ndarray:
        n_in = len(data)
        exact_out = n_in * self.ratio + self.frac
        n_out = int(exact_out)
        self.frac = exact_out - n_out
        if n_out == 0:
            return np.empty((0,) + data.shape[1:], dtype=data.dtype)
        indices = np.linspace(0, n_in - 1, n_out)
        if data.ndim == 1:
            return np.interp(indices, np.arange(n_in), data).astype(data.dtype)
        result = np.empty((n_out, data.shape[1]), dtype=data.dtype)
        for ch in range(data.shape[1]):
            result[:, ch] = np.interp(indices, np.arange(n_in), data[:, ch])
        return result


def downmix_to_mono(data: np.ndarray) -> np.ndarray:
    """Average all channels to mono. Returns (n_samples, 1)."""
    if data.ndim == 1:
        return data.reshape(-1, 1)
    return data.mean(axis=1, keepdims=True)


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_shutdown = threading.Event()
_RETRY_INTERVAL = 5.0
_DEVICE_CHECK_INTERVAL = 2.0  # how often to check if target still exists
_MAX_STREAM_SECONDS = 1800.0  # force-cycle streams to prevent silent stalls

# Per-target queues, only populated when target is actively streaming.
# Guarded by _queues_lock.
_target_queues: dict[str, Queue] = {}
_queues_lock = threading.Lock()

# Per-target software volume (0.0 to 1.0), read from speaker-settings.json.
# Updated periodically by the settings-refresh loop.
_target_volumes: dict[str, float] = {}
_volumes_lock = threading.Lock()

# Event: set by output threads when they detect a device via subprocess
# that isn't in the cached list.  The input reader closes briefly so
# PortAudio can reinit.
_reinit_needed = threading.Event()


def _refresh_volumes() -> None:
    """Periodically read speaker-settings.json and update per-target volumes."""
    while not _shutdown.is_set():
        try:
            spk = _load_speaker_settings()
            with _volumes_lock:
                for slug, entry in spk.items():
                    _target_volumes[slug] = max(0.0, min(1.0, entry.get("volume", 100) / 100.0))
        except Exception:
            pass
        _shutdown.wait(0.5)


def _refresh_portaudio() -> None:
    """Terminate and reinitialize PortAudio to pick up new devices.

    CoreAudio can throw '!obj' or other errors when devices are mid-hotplug.
    Retry a few times, and if it still fails, just continue — the output
    writer threads will retry individually with subprocess-based discovery.
    """
    for attempt in range(3):
        log.info("refreshing PortAudio device list (attempt %d/3)", attempt + 1)
        try:
            sd._terminate()
            sd._initialize()
            # Verify it actually works by querying devices
            sd.query_devices()
            log.info("PortAudio refresh succeeded")
            return
        except Exception:
            log.warning("PortAudio refresh attempt %d failed", attempt + 1, exc_info=True)
            import time
            time.sleep(1.0)

    # All retries failed — reinitialize one last time without verification
    log.warning("PortAudio refresh failed after 3 attempts, continuing anyway")
    try:
        sd._initialize()
    except Exception:
        log.error("PortAudio reinit failed completely — streams may not work until restart",
                  exc_info=True)


# ---------------------------------------------------------------------------
# Input reader thread
# ---------------------------------------------------------------------------


def _input_reader(source_name: str, source_rate: int, source_channels: int) -> None:
    """Read from CaptureAudio and distribute frames to all active target queues."""
    blocksize = settings.audio_forward_blocksize

    while not _shutdown.is_set():
        # Refresh PortAudio if requested by an output thread
        if _reinit_needed.is_set():
            try:
                _refresh_portaudio()
            except Exception:
                log.error("PortAudio refresh crashed — will retry on next cycle",
                          exc_info=True)
            _reinit_needed.clear()

        source_idx = find_device(source_name, kind="input")
        if source_idx is None:
            log.warning("source '%s' not found, retrying in %.0fs",
                        source_name, _RETRY_INTERVAL)
            _shutdown.wait(_RETRY_INTERVAL)
            continue

        log.info("input reader: %s (idx %d, %dch @ %d Hz)",
                 source_name, source_idx, source_channels, source_rate)

        try:
            with sd.InputStream(
                device=source_idx,
                samplerate=source_rate,
                channels=source_channels,
                blocksize=blocksize,
                dtype="float32",
                latency="low",
            ) as stream:
                log.info("input reader active")
                while not _shutdown.is_set() and not _reinit_needed.is_set():
                    data, overflowed = stream.read(blocksize)
                    if overflowed:
                        log.debug("input overflow")
                    with _queues_lock:
                        for q in _target_queues.values():
                            try:
                                q.put_nowait(data.copy())
                            except Full:
                                # Drop oldest frame to keep latency bounded
                                try:
                                    q.get_nowait()
                                except Empty:
                                    pass
                                try:
                                    q.put_nowait(data.copy())
                                except Full:
                                    pass
                if _reinit_needed.is_set():
                    log.info("input reader: closing for PortAudio reinit")
        except sd.PortAudioError as exc:
            if _shutdown.is_set():
                break
            log.error("input PortAudio error: %s — retrying in %.0fs",
                      exc, _RETRY_INTERVAL)
            _shutdown.wait(_RETRY_INTERVAL)
        except Exception:
            if _shutdown.is_set():
                break
            log.exception("input error — retrying in %.0fs", _RETRY_INTERVAL)
            _shutdown.wait(_RETRY_INTERVAL)

    log.info("input reader stopped")


# ---------------------------------------------------------------------------
# Per-target output writer thread
# ---------------------------------------------------------------------------


def _output_writer(
    target_name: str,
    source_rate: int,
    source_channels: int,
) -> None:
    """Output loop for a single target device.

    Waits for the device to appear, opens an OutputStream at the device's
    native rate/channels, reads frames from its queue, resamples/downmixes
    as needed, and writes.  Periodically checks if the device is still
    connected; if not, tears down and retries.
    """
    while not _shutdown.is_set():
        # Check speaker-settings — pause if disabled
        if AUTO_DISCOVER:
            slug = _slugify(target_name)
            spk = _load_speaker_settings()
            if slug in spk and not spk[slug].get("enabled", True):
                _shutdown.wait(DISCOVERY_INTERVAL)
                continue

        # Wait during reinit
        if _reinit_needed.is_set():
            _shutdown.wait(1.0)
            continue

        # Find target device — verify with subprocess to avoid stale cache
        target_idx = find_device(target_name, kind="output")
        if target_idx is not None:
            # Cache says it exists — verify it's actually there
            if not check_device_exists_subprocess(target_name, "output"):
                log.info("[%s] stale cache entry (device gone), waiting", target_name)
                _shutdown.wait(_RETRY_INTERVAL)
                continue
        else:
            # Not in cached list — check with fresh PortAudio
            if check_device_exists_subprocess(target_name, "output"):
                log.info("[%s] detected via subprocess, requesting PortAudio reinit",
                         target_name)
                _reinit_needed.set()
                _shutdown.wait(2.0)
                continue
            log.info("[%s] not connected, retrying in %.0fs",
                     target_name, _RETRY_INTERVAL)
            _shutdown.wait(_RETRY_INTERVAL)
            continue

        tgt_info = sd.query_devices(target_idx)
        target_rate = int(tgt_info["default_samplerate"])
        target_channels = min(source_channels, tgt_info["max_output_channels"])
        needs_resample = source_rate != target_rate
        needs_downmix = source_channels > target_channels

        log.info("[%s] opening: %s (idx %d, %dch @ %d Hz)%s%s",
                 target_name, tgt_info["name"], target_idx,
                 target_channels, target_rate,
                 " [resample]" if needs_resample else "",
                 " [downmix]" if needs_downmix else "")

        # Create streaming resampler (tracks fractional samples across blocks)
        resampler = None
        if needs_resample:
            resampler = StreamingResampler(source_rate, target_rate)

        # Register queue so input reader feeds us frames
        # Queue bounds latency: 12 * blocksize/rate ≈ 32ms at 128/48kHz
        q: Queue = Queue(maxsize=12)
        with _queues_lock:
            _target_queues[target_name] = q

        # Background watchdog: checks device existence without blocking writes
        device_gone = threading.Event()

        def _watchdog():
            while not _shutdown.is_set() and not device_gone.is_set():
                _shutdown.wait(_DEVICE_CHECK_INTERVAL)
                if device_gone.is_set() or _shutdown.is_set():
                    break
                if not check_device_exists_subprocess(target_name, "output"):
                    log.warning("[%s] watchdog: device disappeared", target_name)
                    device_gone.set()

        watchdog = threading.Thread(target=_watchdog, name=f"wd-{target_name}", daemon=True)
        watchdog.start()

        try:
            with sd.OutputStream(
                device=target_idx,
                samplerate=target_rate,
                channels=target_channels,
                dtype="float32",
                latency=0.05,
            ) as stream:
                log.info("[%s] active", target_name)
                # Use the stream's actual blocksize for silence frames
                silence_blocksize = stream.blocksize or settings.audio_forward_blocksize
                primed = False  # True once first real audio arrives
                stream_opened_at = time.monotonic()

                while not _shutdown.is_set() and not _reinit_needed.is_set() and not device_gone.is_set():
                    # Force-cycle long-lived streams to prevent silent stalls
                    # where CoreAudio stops routing audio but PortAudio doesn't
                    # report any error.
                    stream_age = time.monotonic() - stream_opened_at
                    if stream_age > _MAX_STREAM_SECONDS:
                        log.info("[%s] cycling stream after %.0fs to prevent stale output",
                                 target_name, stream_age)
                        break

                    try:
                        data = q.get(timeout=0.1)
                    except Empty:
                        if not primed:
                            # Before first real data, write silence to prevent
                            # CoreAudio underrun replaying stale buffer contents
                            try:
                                stream.write(np.zeros((silence_blocksize, target_channels), dtype="float32"))
                            except sd.PortAudioError:
                                device_gone.set()
                        # Once primed, the 50ms output buffer handles brief gaps
                        continue
                    primed = True

                    # Channel downmix (stereo -> mono)
                    if needs_downmix:
                        data = downmix_to_mono(data)

                    # Resample (48kHz -> target rate) — streaming, stateful
                    if resampler is not None:
                        data = resampler.process(data)

                    # Apply software volume from speaker-settings.json
                    slug = _slugify(target_name)
                    with _volumes_lock:
                        vol = _target_volumes.get(slug, 1.0)
                    if vol < 0.999:
                        data = data * vol

                    try:
                        stream.write(data)
                    except sd.PortAudioError:
                        log.warning("[%s] write error, closing stream", target_name)
                        device_gone.set()
                        continue

                    # Detect silently dead streams: CoreAudio may stop the
                    # PortAudio stream without raising an error on write.
                    if not stream.active:
                        log.warning("[%s] stream no longer active (CoreAudio killed it), reconnecting",
                                    target_name)
                        device_gone.set()

        except sd.PortAudioError as exc:
            if _shutdown.is_set():
                break
            # Device exists but can't open — stale PortAudio state, reinit needed
            if check_device_exists_subprocess(target_name, "output"):
                log.info("[%s] PortAudio error but device exists — requesting reinit",
                         target_name)
                _reinit_needed.set()
            else:
                log.error("[%s] PortAudio error: %s — retrying in %.0fs",
                          target_name, exc, _RETRY_INTERVAL)
        except Exception:
            if _shutdown.is_set():
                break
            log.exception("[%s] unexpected error — retrying in %.0fs",
                          target_name, _RETRY_INTERVAL)
        finally:
            device_gone.set()  # stop watchdog thread
            with _queues_lock:
                _target_queues.pop(target_name, None)

        if not _shutdown.is_set():
            if _reinit_needed.is_set():
                # Reinit in progress — wait briefly then reopen quickly
                _shutdown.wait(1.0)
            else:
                _shutdown.wait(_RETRY_INTERVAL)

    log.info("[%s] stopped", target_name)


# ---------------------------------------------------------------------------
# Output discovery thread
# ---------------------------------------------------------------------------


def _output_discovery_thread(source_rate: int, source_channels: int) -> None:
    """Periodically scan for new output devices and start writer threads."""
    while not _shutdown.is_set():
        _shutdown.wait(DISCOVERY_INTERVAL)
        if _shutdown.is_set():
            break
        try:
            discovered = _discover_output_devices_subprocess()
            speaker_settings = _load_speaker_settings()
            settings_changed = False

            for dev_name in discovered:
                slug = _slugify(dev_name)

                # Check speaker-settings — skip if disabled
                if slug in speaker_settings and not speaker_settings[slug].get("enabled", True):
                    continue

                # Auto-populate speaker settings
                if slug not in speaker_settings:
                    speaker_settings[slug] = {"enabled": True}
                    settings_changed = True

                with _output_threads_lock:
                    if dev_name in _output_threads and _output_threads[dev_name].is_alive():
                        continue

                # New device found
                log.info("discovered new output: %s [%s]", dev_name, slug)
                t = threading.Thread(
                    target=_output_writer,
                    args=(dev_name, source_rate, source_channels),
                    name=f"out-{dev_name}",
                    daemon=True,
                )
                t.start()
                with _output_threads_lock:
                    _output_threads[dev_name] = t
                log.info("started output thread for discovered '%s'", dev_name)

            if settings_changed:
                _save_speaker_settings(speaker_settings)
        except Exception:
            log.debug("output discovery thread error", exc_info=True)

    log.info("output discovery thread stopped")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    if not settings.audio_forward_enabled:
        log.info("audio forwarding disabled (AUDIO_ASSIST_AUDIO_FORWARD_ENABLED=false)")
        return

    source_name = settings.audio_forward_source_device
    source_rate = settings.audio_forward_sample_rate
    source_channels = settings.audio_forward_channels
    target_names = [t.strip() for t in settings.audio_forward_target_devices.split(",") if t.strip()]

    log.info("audio-forward starting")
    log.info("  source: %s (%dch @ %d Hz)", source_name, source_channels, source_rate)
    log.info("  seed targets: %s", target_names)
    log.info("  auto-discover: %s", AUTO_DISCOVER)

    # If auto-discover, also scan for devices now
    if AUTO_DISCOVER:
        speaker_settings = _load_speaker_settings()
        discovered = _discover_output_devices_subprocess()
        settings_changed = False
        for dev_name in discovered:
            slug = _slugify(dev_name)
            # Auto-populate speaker settings
            if slug not in speaker_settings:
                speaker_settings[slug] = {"enabled": True}
                settings_changed = True
            # Skip disabled devices
            if not speaker_settings[slug].get("enabled", True):
                continue
            if dev_name not in target_names:
                target_names.append(dev_name)
                log.info("  discovered: %s", dev_name)
        if settings_changed:
            _save_speaker_settings(speaker_settings)

    if not target_names:
        log.error("no target devices configured")
        return

    def _handle_signal(signum, frame):
        sig = signal.Signals(signum).name
        log.info("received %s, shutting down", sig)
        _shutdown.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Volume refresh thread (reads speaker-settings.json every 0.5s)
    vol_thread = threading.Thread(
        target=_refresh_volumes,
        name="volume-refresh",
        daemon=True,
    )
    vol_thread.start()
    log.info("started volume refresh thread")

    # Input reader thread (single reader for CaptureAudio)
    input_thread = threading.Thread(
        target=_input_reader,
        args=(source_name, source_rate, source_channels),
        name="input-reader",
        daemon=True,
    )
    input_thread.start()
    log.info("started input reader for '%s'", source_name)

    # One output thread per target device
    output_threads: list[threading.Thread] = []
    for target_name in target_names:
        t = threading.Thread(
            target=_output_writer,
            args=(target_name, source_rate, source_channels),
            name=f"out-{target_name}",
            daemon=True,
        )
        t.start()
        output_threads.append(t)
        with _output_threads_lock:
            _output_threads[target_name] = t
        log.info("started output thread for '%s'", target_name)

    # Discovery thread (if auto-discover enabled)
    if AUTO_DISCOVER:
        t = threading.Thread(
            target=_output_discovery_thread,
            args=(source_rate, source_channels),
            name="output-discovery",
            daemon=True,
        )
        t.start()
        output_threads.append(t)
        log.info("started output discovery thread (interval=%.0fs)", DISCOVERY_INTERVAL)

    # Wait for shutdown signal
    _shutdown.wait()

    # Wait for threads to finish
    for t in [input_thread] + output_threads:
        t.join(timeout=3.0)

    log.info("audio-forward exiting")


if __name__ == "__main__":
    main()
