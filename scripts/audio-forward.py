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

import logging
import os
import signal
import sys
import threading
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
    import subprocess
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


def resample(data: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Resample audio data using linear interpolation.

    Handles both mono (n_samples,) and multi-channel (n_samples, channels).
    """
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

# Per-target queues, only populated when target is actively streaming.
# Guarded by _queues_lock.
_target_queues: dict[str, Queue] = {}
_queues_lock = threading.Lock()

# Event: set by output threads when they detect a device via subprocess
# that isn't in the cached list.  The input reader closes briefly so
# PortAudio can reinit.
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
# Input reader thread
# ---------------------------------------------------------------------------


def _input_reader(source_name: str, source_rate: int, source_channels: int) -> None:
    """Read from CaptureAudio and distribute frames to all active target queues."""
    blocksize = settings.audio_forward_blocksize

    while not _shutdown.is_set():
        # Refresh PortAudio if requested by an output thread
        if _reinit_needed.is_set():
            _refresh_portaudio()
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
                                pass  # target can't keep up, drop frame
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

        # Register queue so input reader feeds us frames
        q: Queue = Queue(maxsize=200)
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
                latency=settings.audio_forward_latency,
            ) as stream:
                log.info("[%s] active", target_name)

                while not _shutdown.is_set() and not _reinit_needed.is_set() and not device_gone.is_set():
                    try:
                        data = q.get(timeout=0.5)
                    except Empty:
                        continue

                    # Channel downmix (stereo -> mono)
                    if needs_downmix:
                        data = downmix_to_mono(data)

                    # Resample (48kHz -> target rate)
                    if needs_resample:
                        data = resample(data, source_rate, target_rate)

                    try:
                        stream.write(data)
                    except sd.PortAudioError:
                        log.warning("[%s] write error, closing stream", target_name)
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

    if not target_names:
        log.error("no target devices configured")
        return

    log.info("audio-forward starting")
    log.info("  source: %s (%dch @ %d Hz)", source_name, source_channels, source_rate)
    log.info("  targets: %s", target_names)

    def _handle_signal(signum, frame):
        sig = signal.Signals(signum).name
        log.info("received %s, shutting down", sig)
        _shutdown.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

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
        log.info("started output thread for '%s'", target_name)

    # Wait for shutdown signal
    _shutdown.wait()

    # Wait for threads to finish
    for t in [input_thread] + output_threads:
        t.join(timeout=3.0)

    log.info("audio-forward exiting")


if __name__ == "__main__":
    main()
