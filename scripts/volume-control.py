#!/usr/bin/env python3
"""Multi-device volume control via CoreAudio HAL API.

Controls volume directly on audio devices without switching
the system output device. No audio glitches.

Runs on port 8788.
"""

import ctypes
import ctypes.util
import json
import os
import struct
import subprocess
import sys
import tempfile
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_float,
    c_int32,
    c_uint32,
    c_void_p,
    create_string_buffer,
    sizeof,
)
from http.server import BaseHTTPRequestHandler, HTTPServer

import threading
import time

PORT = 8788

EXCLUDED_SUBSTRINGS = ["CaptureMic", "CaptureAudio", "BlackHole",
                       "Aggregate Device", "Multi-Output Device", "ZoomAudioDevice",
                       "Cluely", "Microsoft Teams Audio"]

# --- CoreAudio ctypes bindings ---

AudioObjectID = c_uint32
OSStatus = c_int32


class AudioObjectPropertyAddress(Structure):
    _fields_ = [
        ("mSelector", c_uint32),
        ("mScope", c_uint32),
        ("mElement", c_uint32),
    ]


def _fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode("ascii"))[0]


kAudioObjectSystemObject = 1
kAudioHardwarePropertyDevices = _fourcc("dev#")
kAudioObjectPropertyScopeGlobal = _fourcc("glob")
kAudioObjectPropertyScopeOutput = _fourcc("outp")
kAudioObjectPropertyElementMain = 0
kAudioObjectPropertyName = _fourcc("lnam")
kAudioDevicePropertyVolumeScalar = _fourcc("volm")
kAudioDevicePropertyMute = _fourcc("mute")

_ca = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
)
_ca.AudioObjectGetPropertyDataSize.argtypes = [
    AudioObjectID, POINTER(AudioObjectPropertyAddress),
    c_uint32, c_void_p, POINTER(c_uint32),
]
_ca.AudioObjectGetPropertyDataSize.restype = OSStatus
_ca.AudioObjectGetPropertyData.argtypes = [
    AudioObjectID, POINTER(AudioObjectPropertyAddress),
    c_uint32, c_void_p, POINTER(c_uint32), c_void_p,
]
_ca.AudioObjectGetPropertyData.restype = OSStatus
_ca.AudioObjectSetPropertyData.argtypes = [
    AudioObjectID, POINTER(AudioObjectPropertyAddress),
    c_uint32, c_void_p, c_uint32, c_void_p,
]
_ca.AudioObjectSetPropertyData.restype = OSStatus
_ca.AudioObjectHasProperty.argtypes = [
    AudioObjectID, POINTER(AudioObjectPropertyAddress),
]
_ca.AudioObjectHasProperty.restype = c_int32

_cf = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)
_cf.CFStringGetLength.argtypes = [c_void_p]
_cf.CFStringGetLength.restype = c_int32
_cf.CFStringGetCString.argtypes = [c_void_p, ctypes.c_char_p, c_int32, c_uint32]
_cf.CFStringGetCString.restype = ctypes.c_bool
_cf.CFRelease.argtypes = [c_void_p]
_cf.CFRelease.restype = None

kCFStringEncodingUTF8 = 0x08000100


def _cfstring_to_str(cfstr: c_void_p) -> str:
    if not cfstr:
        return ""
    buf = create_string_buffer(512)
    if _cf.CFStringGetCString(cfstr, buf, 512, kCFStringEncodingUTF8):
        return buf.value.decode("utf-8", errors="replace")
    return ""


def _get_all_device_ids() -> list[int]:
    addr = AudioObjectPropertyAddress(
        kAudioHardwarePropertyDevices,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    size = c_uint32(0)
    if _ca.AudioObjectGetPropertyDataSize(
        kAudioObjectSystemObject, byref(addr), 0, None, byref(size)
    ) != 0:
        return []
    count = size.value // sizeof(AudioObjectID)
    ids = (AudioObjectID * count)()
    if _ca.AudioObjectGetPropertyData(
        kAudioObjectSystemObject, byref(addr), 0, None, byref(size), byref(ids)
    ) != 0:
        return []
    return [ids[i] for i in range(count)]


def _get_device_name(device_id: int) -> str:
    addr = AudioObjectPropertyAddress(
        kAudioObjectPropertyName,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    cfstr = c_void_p(0)
    size = c_uint32(sizeof(c_void_p))
    if _ca.AudioObjectGetPropertyData(
        AudioObjectID(device_id), byref(addr), 0, None, byref(size), byref(cfstr)
    ) != 0 or not cfstr.value:
        return ""
    name = _cfstring_to_str(cfstr)
    _cf.CFRelease(cfstr)
    return name


def _has_output_volume(device_id: int, channel: int) -> bool:
    addr = AudioObjectPropertyAddress(
        kAudioDevicePropertyVolumeScalar,
        kAudioObjectPropertyScopeOutput,
        channel,
    )
    return bool(_ca.AudioObjectHasProperty(AudioObjectID(device_id), byref(addr)))


def _find_device_with_output_volume(name: str) -> tuple[int, list[int]] | None:
    """Find a device by name that has output volume control.
    Returns (device_id, [channels_with_volume]) or None.
    """
    for did in _get_all_device_ids():
        if _get_device_name(did) != name:
            continue
        channels = [ch for ch in (0, 1, 2) if _has_output_volume(did, ch)]
        if channels:
            return (did, channels)
    return None


def _get_volume(device_id: int, channels: list[int]) -> float | None:
    for ch in channels:
        addr = AudioObjectPropertyAddress(
            kAudioDevicePropertyVolumeScalar,
            kAudioObjectPropertyScopeOutput,
            ch,
        )
        vol = c_float(0.0)
        size = c_uint32(sizeof(c_float))
        if _ca.AudioObjectGetPropertyData(
            AudioObjectID(device_id), byref(addr), 0, None, byref(size), byref(vol)
        ) == 0:
            return round(vol.value * 100)
    return None


def _set_volume(device_id: int, channels: list[int], percent: int) -> bool:
    scalar = max(0.0, min(1.0, percent / 100.0))
    ok = False
    for ch in channels:
        addr = AudioObjectPropertyAddress(
            kAudioDevicePropertyVolumeScalar,
            kAudioObjectPropertyScopeOutput,
            ch,
        )
        vol = c_float(scalar)
        if _ca.AudioObjectSetPropertyData(
            AudioObjectID(device_id), byref(addr), 0, None,
            sizeof(c_float), byref(vol),
        ) == 0:
            ok = True
    return ok


def _get_mute(device_id: int) -> bool | None:
    addr = AudioObjectPropertyAddress(
        kAudioDevicePropertyMute, kAudioObjectPropertyScopeOutput, 0,
    )
    if not _ca.AudioObjectHasProperty(AudioObjectID(device_id), byref(addr)):
        return None
    muted = c_uint32(0)
    size = c_uint32(sizeof(c_uint32))
    if _ca.AudioObjectGetPropertyData(
        AudioObjectID(device_id), byref(addr), 0, None, byref(size), byref(muted)
    ) != 0:
        return None
    return bool(muted.value)


def _set_mute(device_id: int, muted: bool) -> bool:
    addr = AudioObjectPropertyAddress(
        kAudioDevicePropertyMute, kAudioObjectPropertyScopeOutput, 0,
    )
    if not _ca.AudioObjectHasProperty(AudioObjectID(device_id), byref(addr)):
        return False
    val = c_uint32(1 if muted else 0)
    return _ca.AudioObjectSetPropertyData(
        AudioObjectID(device_id), byref(addr), 0, None,
        sizeof(c_uint32), byref(val),
    ) == 0


# --- Dynamic device discovery ---

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
MIC_SETTINGS_PATH = os.path.join(_PROJECT_ROOT, "data", "mic-settings.json")
MIC_LEVELS_PATH = os.path.join(_PROJECT_ROOT, "data", "mic-levels.json")
SPEAKER_SETTINGS_PATH = os.path.join(_PROJECT_ROOT, "data", "speaker-settings.json")
MIC_TEST_SCRIPT = os.path.join(_SCRIPT_DIR, "mic-test.py")
_mic_test_proc = None  # track running test subprocess


def _discover_output_devices() -> dict[str, dict]:
    """Find all physical output devices with volume control."""
    devices = {}
    for did in _get_all_device_ids():
        name = _get_device_name(did)
        if not name or any(ex.lower() in name.lower() for ex in EXCLUDED_SUBSTRINGS):
            continue
        channels = [ch for ch in (0, 1, 2) if _has_output_volume(did, ch)]
        if channels:
            slug = name.lower().replace(" ", "-")
            icon = "\U0001f3a7" if any(kw in name.lower() for kw in ("headphone", "bose", "airpod")) else "\U0001f50a"
            devices[slug] = {"slug": slug, "display": name, "dev_name": name, "icon": icon}
    return devices


def _discover_mic_devices() -> dict[str, dict]:
    """Build mic device list from data/mic-settings.json (populated by mic-forward)."""
    settings = _load_mic_settings()
    devices = {}
    for slug, s in settings.items():
        display = slug.replace("-", " ").title()
        devices[slug] = {"slug": slug, "display": display, "icon": "\U0001f3a4"}
    return devices


_devices: dict[str, dict] = {}
_mic_devices: dict[str, dict] = {}
_devices_lock = threading.Lock()


def _refresh_devices():
    """Periodically re-discover devices."""
    global _devices, _mic_devices
    while True:
        time.sleep(10)
        try:
            new_devs = _discover_output_devices()
            new_mics = _discover_mic_devices()
            with _devices_lock:
                _devices = new_devs
                _mic_devices = new_mics
        except Exception:
            pass


def _load_mic_settings() -> dict[str, dict]:
    """Load mic settings from JSON file, returning {slug: {volume, enabled}}."""
    try:
        with open(MIC_SETTINGS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _load_speaker_settings() -> dict[str, dict]:
    """Load speaker settings from JSON file."""
    try:
        with open(SPEAKER_SETTINGS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_speaker_settings(data: dict[str, dict]) -> None:
    """Atomically write speaker settings to JSON file."""
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
        raise


def _save_mic_settings(settings: dict[str, dict]) -> None:
    """Atomically write mic settings to JSON file."""
    os.makedirs(os.path.dirname(MIC_SETTINGS_PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(MIC_SETTINGS_PATH), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")
        os.replace(tmp, MIC_SETTINGS_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _resolve_device(slug: str) -> dict | None:
    """Look up a device by slug, resolving the current CoreAudio device ID by name.
    Returns dict with device_id and channels, or None if device not found.
    """
    dev = _devices.get(slug)
    if not dev:
        return None
    result = _find_device_with_output_volume(dev["dev_name"])
    if not result:
        return None
    device_id, channels = result
    return {**dev, "device_id": device_id, "channels": channels}

# --- HTML ---

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Volume Control</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    background: #1a1a1a; color: #e0e0e0;
    display: flex; justify-content: center; align-items: flex-start;
    min-height: 100vh; gap: 20px; flex-wrap: wrap; padding: 20px;
  }
  .section-title { width: 100%; text-align: center; font-size: 13px; color: #666;
    text-transform: uppercase; letter-spacing: 2px; margin: 10px 0 0; }
  .card {
    background: #2a2a2a; border-radius: 16px; padding: 28px;
    width: 300px; box-shadow: 0 8px 32px rgba(0,0,0,0.4);
  }
  .card.disconnected { opacity: 0.4; }
  .card.disabled { opacity: 0.3; }
  .header { display: flex; align-items: center; justify-content: center; gap: 10px; margin-bottom: 20px; }
  .header .icon { font-size: 24px; }
  .header h2 { font-size: 16px; color: #fff; }
  .volume-display {
    font-size: 56px; font-weight: 700; text-align: center;
    color: #4af; margin: 12px 0 6px; transition: color 0.2s;
  }
  .volume-display.muted { color: #c33; }
  .device-label { text-align: center; color: #666; font-size: 11px; margin-bottom: 16px; }
  .slider-row { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
  .slider-row span { font-size: 16px; cursor: pointer; user-select: none; }
  input[type=range] {
    flex: 1; -webkit-appearance: none; height: 6px;
    border-radius: 3px; background: #444; outline: none;
  }
  input[type=range]::-webkit-slider-thumb {
    -webkit-appearance: none; width: 22px; height: 22px;
    border-radius: 50%; background: #4af; cursor: pointer;
    box-shadow: 0 2px 6px rgba(0,0,0,0.3);
  }
  .presets { display: flex; gap: 5px; margin-bottom: 12px; }
  .presets button {
    flex: 1; padding: 7px 2px; font-size: 12px; font-weight: 500;
    border: 1px solid #444; border-radius: 8px;
    background: #2a2a2a; color: #aaa; cursor: pointer;
  }
  .presets button:hover { background: #383838; color: #fff; border-color: #4af; }
  .buttons { display: flex; gap: 6px; margin-bottom: 10px; }
  .buttons button {
    flex: 1; padding: 12px; font-size: 16px; font-weight: 600;
    border: none; border-radius: 10px; cursor: pointer;
    transition: background 0.12s;
  }
  .btn-down { background: #333; color: #e0e0e0; }
  .btn-down:hover { background: #444; }
  .btn-down:active { background: #555; }
  .btn-up { background: #333; color: #e0e0e0; }
  .btn-up:hover { background: #444; }
  .btn-up:active { background: #555; }
  .btn-mute {
    width: 100%; padding: 10px; font-size: 13px; font-weight: 600;
    border: none; border-radius: 10px; cursor: pointer;
    background: #333; color: #e0e0e0; transition: background 0.12s;
    margin-bottom: 6px;
  }
  .btn-mute:hover { background: #444; }
  .btn-mute.muted { background: #c33; color: #fff; }
  .btn-enable {
    width: 100%; padding: 8px; font-size: 11px; font-weight: 500;
    border: 1px solid #444; border-radius: 8px; cursor: pointer;
    background: #2a2a2a; color: #888; transition: all 0.12s;
  }
  .btn-enable:hover { border-color: #4af; color: #fff; }
  .btn-enable.disabled-route { background: #3a2020; border-color: #c33; color: #c33; }
  .status { text-align: center; color: #555; font-size: 10px; margin-top: 10px; }
  .mic-card {
    background: #2a2a2a; border-radius: 16px; padding: 20px;
    width: 300px; box-shadow: 0 8px 32px rgba(0,0,0,0.4);
  }
  .mic-card .header { margin-bottom: 12px; }
  .level-bar-bg { background: #333; border-radius: 4px; height: 8px; margin: 8px 0; }
  .level-bar { background: #4a4; border-radius: 4px; height: 8px; min-width: 2px;
    transition: width 0.15s; }
  .level-bar.hot { background: #c33; }
  .mic-vol { font-size: 14px; text-align: center; color: #aaa; margin: 4px 0; }
  .keys { text-align: center; color: #444; font-size: 10px; margin-top: 24px; width: 100%; }
  kbd { background: #333; padding: 2px 6px; border-radius: 3px; font-size: 10px; }
</style>
</head>
<body>
<div class="section-title" id="speakers-title">Speakers</div>
<div id="speakers-container" style="display:flex;gap:20px;flex-wrap:wrap;justify-content:center;width:100%"></div>
<div class="section-title" id="mics-title">Microphones</div>
<div id="mics-container" style="display:flex;gap:20px;flex-wrap:wrap;justify-content:center;width:100%"></div>
<div class="keys" id="keys-hint"></div>
<script>
let deviceSlugs = [];
const state = {};
let speakerSettings = {};

function createCard(dev, container) {
  if (document.getElementById('card-' + dev.slug)) return;
  const card = document.createElement('div');
  card.className = 'card';
  card.id = 'card-' + dev.slug;
  card.innerHTML = `
    <div class="header">
      <span class="icon">${dev.icon}</span>
      <h2>${dev.display}</h2>
    </div>
    <div class="volume-display" id="vol-${dev.slug}">--</div>
    <div class="device-label">Direct CoreAudio</div>
    <div class="slider-row">
      <span onclick="nudge('${dev.slug}', -5)">🔈</span>
      <input type="range" id="slider-${dev.slug}" min="0" max="100" value="50" />
      <span onclick="nudge('${dev.slug}', 5)">🔊</span>
    </div>
    <div class="presets">
      <button onclick="setVol('${dev.slug}', 20)">20</button>
      <button onclick="setVol('${dev.slug}', 40)">40</button>
      <button onclick="setVol('${dev.slug}', 60)">60</button>
      <button onclick="setVol('${dev.slug}', 80)">80</button>
      <button onclick="setVol('${dev.slug}', 100)">Max</button>
    </div>
    <div class="buttons">
      <button class="btn-down" onclick="nudge('${dev.slug}', -10)">− 10</button>
      <button class="btn-up" onclick="nudge('${dev.slug}', +10)">+ 10</button>
    </div>
    <button class="btn-mute" id="mute-${dev.slug}" onclick="toggleMute('${dev.slug}')">Mute</button>
    <button class="btn-enable" id="enable-${dev.slug}" onclick="toggleEnable('${dev.slug}')">Routing: On</button>
    <div class="status" id="status-${dev.slug}">Connecting...</div>
  `;
  container.appendChild(card);

  const slider = document.getElementById('slider-' + dev.slug);
  let debounce = null;
  slider.addEventListener('input', () => {
    document.getElementById('vol-' + dev.slug).textContent = slider.value;
    clearTimeout(debounce);
    debounce = setTimeout(() => setVol(dev.slug, parseInt(slider.value)), 80);
  });

  if (!state[dev.slug]) state[dev.slug] = { current: 50, preMute: 50, muted: false };
}

function createMicCard(mic, container) {
  if (document.getElementById('mic-' + mic.slug)) return;
  const card = document.createElement('div');
  card.className = 'mic-card';
  card.id = 'mic-' + mic.slug;
  card.innerHTML = `
    <div class="header">
      <span class="icon">${mic.icon}</span>
      <h2>${mic.display}</h2>
    </div>
    <div class="mic-vol" id="micvol-${mic.slug}">Gain: ${mic.volume}%</div>
    <div class="slider-row">
      <span>🎤</span>
      <input type="range" id="micslider-${mic.slug}" min="0" max="500" value="${mic.volume}" />
    </div>
    <div class="level-bar-bg"><div class="level-bar" id="miclevel-${mic.slug}"></div></div>
    <div class="status" id="micstatus-${mic.slug}">${mic.enabled ? 'Active' : 'Disabled'}</div>
  `;
  container.appendChild(card);

  const slider = document.getElementById('micslider-' + mic.slug);
  let debounce = null;
  slider.addEventListener('input', () => {
    document.getElementById('micvol-' + mic.slug).textContent = 'Gain: ' + slider.value + '%';
    clearTimeout(debounce);
    debounce = setTimeout(() => setMicVol(mic.slug, parseInt(slider.value)), 150);
  });
}

async function refreshDevices() {
  try {
    const r = await fetch('/api/devices');
    const devices = await r.json();
    const container = document.getElementById('speakers-container');
    const slugs = Object.keys(devices);

    // Remove cards for devices that no longer exist
    container.querySelectorAll('.card').forEach(card => {
      const slug = card.id.replace('card-', '');
      if (!devices[slug]) card.remove();
    });

    slugs.forEach(slug => {
      const d = devices[slug];
      createCard(d, container);
      fetchVol(slug);
    });
    deviceSlugs = slugs;
    updateKeysHint();

    // Refresh speaker settings
    const sr = await fetch('/api/speaker-settings');
    speakerSettings = await sr.json();
    for (const slug of slugs) {
      const btn = document.getElementById('enable-' + slug);
      if (!btn) continue;
      const enabled = speakerSettings[slug] ? speakerSettings[slug].enabled !== false : true;
      btn.textContent = enabled ? 'Routing: On' : 'Routing: Off';
      btn.classList.toggle('disabled-route', !enabled);
      const card = document.getElementById('card-' + slug);
      if (card) card.classList.toggle('disabled', !enabled);
    }
  } catch(e) {}

  try {
    const r = await fetch('/api/mics');
    const mics = await r.json();
    const container = document.getElementById('mics-container');
    const slugs = Object.keys(mics);

    container.querySelectorAll('.mic-card').forEach(card => {
      const slug = card.id.replace('mic-', '');
      if (!mics[slug]) card.remove();
    });

    slugs.forEach(slug => createMicCard(mics[slug], container));
  } catch(e) {}
}

function updateKeysHint() {
  const el = document.getElementById('keys-hint');
  if (deviceSlugs.length === 0) { el.innerHTML = ''; return; }
  let hint = 'Keyboard: <kbd>↑</kbd><kbd>↓</kbd> ' + deviceSlugs[0] + ' ±5';
  if (deviceSlugs.length > 1) hint += ' &nbsp; <kbd>⇧↑</kbd><kbd>⇧↓</kbd> ' + deviceSlugs[1] + ' ±5';
  hint += ' &nbsp; <kbd>M</kbd> mute ' + deviceSlugs[0];
  if (deviceSlugs.length > 1) hint += ' &nbsp; <kbd>⇧M</kbd> mute ' + deviceSlugs[1];
  el.innerHTML = hint;
}

async function fetchVol(slug) {
  try {
    const r = await fetch('/api/volume/' + slug);
    const d = await r.json();
    const card = document.getElementById('card-' + slug);
    if (!card) return;
    if (d.error) {
      card.classList.add('disconnected');
      document.getElementById('status-' + slug).textContent = d.error;
      return;
    }
    card.classList.remove('disconnected');
    const s = state[slug] || (state[slug] = { current: 50, preMute: 50, muted: false });
    s.current = d.volume;
    s.muted = d.muted || false;
    document.getElementById('slider-' + slug).value = s.current;
    document.getElementById('vol-' + slug).textContent = s.current;
    const muteBtn = document.getElementById('mute-' + slug);
    muteBtn.textContent = s.muted ? 'Unmute' : 'Mute';
    muteBtn.classList.toggle('muted', s.muted);
    document.getElementById('vol-' + slug).classList.toggle('muted', s.muted);
    document.getElementById('status-' + slug).textContent = 'Connected';
  } catch(e) {
    const st = document.getElementById('status-' + slug);
    if (st) st.textContent = 'Disconnected';
  }
}

async function setVol(slug, v) {
  v = Math.max(0, Math.min(100, Math.round(v)));
  const s = state[slug] || (state[slug] = { current: 50, preMute: 50, muted: false });
  s.current = v;
  if (v > 0) { s.muted = false; s.preMute = v; }
  const slider = document.getElementById('slider-' + slug);
  if (slider) slider.value = v;
  const vol = document.getElementById('vol-' + slug);
  if (vol) vol.textContent = v;
  const muteBtn = document.getElementById('mute-' + slug);
  if (muteBtn) {
    muteBtn.textContent = s.muted ? 'Unmute' : 'Mute';
    muteBtn.classList.toggle('muted', s.muted);
  }
  if (vol) vol.classList.toggle('muted', s.muted);
  try {
    await fetch('/api/volume/' + slug, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({volume: v})
    });
  } catch(e) {}
}

function nudge(slug, delta) {
  const s = state[slug];
  if (s) setVol(slug, s.current + delta);
}

function toggleMute(slug) {
  const s = state[slug];
  if (!s) return;
  if (s.muted) { setVol(slug, s.preMute || 50); }
  else { s.preMute = s.current; setVol(slug, 0); }
}

async function toggleEnable(slug) {
  const current = speakerSettings[slug] ? speakerSettings[slug].enabled !== false : true;
  const newEnabled = !current;
  try {
    await fetch('/api/speaker/' + slug + '/enable', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: newEnabled})
    });
    speakerSettings[slug] = { enabled: newEnabled };
    const btn = document.getElementById('enable-' + slug);
    btn.textContent = newEnabled ? 'Routing: On' : 'Routing: Off';
    btn.classList.toggle('disabled-route', !newEnabled);
    document.getElementById('card-' + slug).classList.toggle('disabled', !newEnabled);
  } catch(e) {}
}

async function setMicVol(slug, vol) {
  try {
    await fetch('/api/mic/' + slug, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({volume: vol})
    });
  } catch(e) {}
}

async function fetchMicLevels() {
  try {
    const r = await fetch('/api/mic-levels');
    const levels = await r.json();
    for (const [slug, data] of Object.entries(levels)) {
      const bar = document.getElementById('miclevel-' + slug);
      if (!bar) continue;
      // Map dBFS to percentage (0 = -60dB, 100 = 0dB)
      const db = Math.max(-60, Math.min(0, data.level_dbfs || -60));
      const pct = ((db + 60) / 60) * 100;
      bar.style.width = pct + '%';
      bar.classList.toggle('hot', db > -6);
    }
  } catch(e) {}
}

document.addEventListener('keydown', (e) => {
  const idx = e.shiftKey ? 1 : 0;
  const slug = deviceSlugs[idx];
  if (!slug || !state[slug]) return;
  if (e.key === 'ArrowUp' || e.key === 'ArrowRight') { e.preventDefault(); nudge(slug, 5); }
  if (e.key === 'ArrowDown' || e.key === 'ArrowLeft') { e.preventDefault(); nudge(slug, -5); }
  if (e.key === 'm' || e.key === 'M') { toggleMute(slug); }
});

// Initial load + periodic refresh
refreshDevices();
setInterval(refreshDevices, 10000);
setInterval(() => deviceSlugs.forEach(s => fetchVol(s)), 1000);
setInterval(fetchMicLevels, 200);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(HTML.encode())
        elif self.path.startswith("/api/volume/"):
            slug = self.path.split("/")[-1]
            dev = _resolve_device(slug)
            if not dev:
                self._json(404, {"error": f"device '{slug}' not connected"})
                return
            vol = _get_volume(dev["device_id"], dev["channels"])
            m = _get_mute(dev["device_id"])
            self._json(200, {
                "volume": vol,
                "muted": m or False,
                "device": dev["display"],
            })
        elif self.path == "/api/devices":
            with _devices_lock:
                devs = dict(_devices)
            result = {}
            for slug, d in devs.items():
                dev = _resolve_device(slug)
                if dev:
                    result[slug] = {
                        "slug": dev["slug"],
                        "display": dev["display"],
                        "icon": dev["icon"],
                        "volume": _get_volume(dev["device_id"], dev["channels"]),
                        "muted": _get_mute(dev["device_id"]) or False,
                    }
                else:
                    result[slug] = {
                        "slug": d["slug"],
                        "display": d["display"],
                        "icon": d["icon"],
                        "volume": None,
                        "muted": None,
                        "error": "not connected",
                    }
            self._json(200, result)
        elif self.path == "/api/mics":
            with _devices_lock:
                mics = dict(_mic_devices)
            mic_settings = _load_mic_settings()
            result = {}
            for slug, md in mics.items():
                s = mic_settings.get(slug, {"volume": 100, "enabled": True})
                result[slug] = {
                    "slug": md["slug"],
                    "display": md["display"],
                    "icon": md["icon"],
                    "volume": s.get("volume", 100),
                    "enabled": s.get("enabled", True),
                }
            self._json(200, result)
        elif self.path == "/api/mic-levels":
            try:
                with open(MIC_LEVELS_PATH) as f:
                    self._json(200, json.load(f))
            except (FileNotFoundError, json.JSONDecodeError):
                self._json(200, {})
        elif self.path.startswith("/api/mic/"):
            slug = self.path.split("/")[-1]
            with _devices_lock:
                md = _mic_devices.get(slug)
            if not md:
                self._json(404, {"error": f"mic '{slug}' not found"})
                return
            mic_settings = _load_mic_settings()
            s = mic_settings.get(slug, {"volume": 100, "enabled": True})
            self._json(200, {
                "slug": md["slug"],
                "display": md["display"],
                "icon": md["icon"],
                "volume": s.get("volume", 100),
                "enabled": s.get("enabled", True),
            })
        elif self.path == "/api/speaker-settings":
            self._json(200, _load_speaker_settings())
        elif self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.startswith("/api/volume/"):
            slug = self.path.split("/")[-1]
            dev = _resolve_device(slug)
            if not dev:
                self._json(404, {"error": f"device '{slug}' not connected"})
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            target = body.get("volume")
            if target is None:
                self._json(400, {"error": "provide volume 0-100"})
                return
            target = max(0, min(100, int(target)))
            _set_volume(dev["device_id"], dev["channels"], target)
            if target == 0:
                _set_mute(dev["device_id"], True)
            else:
                _set_mute(dev["device_id"], False)
            vol = _get_volume(dev["device_id"], dev["channels"])
            self._json(200, {"volume": vol, "ok": True})
        elif self.path == "/api/volume-step":
            # Adjust all unmuted devices by a relative step (±percentage points)
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            step = body.get("step", 2)
            step = max(-100, min(100, int(step)))
            results = {}
            with _devices_lock:
                devs = dict(_devices)
            for slug, d in devs.items():
                dev = _resolve_device(slug)
                if not dev:
                    continue
                muted = _get_mute(dev["device_id"])
                if muted:
                    continue
                current = _get_volume(dev["device_id"], dev["channels"])
                if current is None:
                    continue
                new_vol = max(0, min(100, int(current + step)))
                _set_volume(dev["device_id"], dev["channels"], new_vol)
                if new_vol == 0:
                    _set_mute(dev["device_id"], True)
                results[slug] = {"volume": new_vol}
            self._json(200, {"ok": True, "step": step, "devices": results})
        elif self.path.startswith("/api/speaker/") and self.path.endswith("/enable"):
            # POST /api/speaker/{slug}/enable — toggle audio-forward routing
            parts = self.path.split("/")
            slug = parts[3]  # /api/speaker/{slug}/enable
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            enabled = body.get("enabled", True)
            spk_settings = _load_speaker_settings()
            spk_settings[slug] = {"enabled": bool(enabled)}
            _save_speaker_settings(spk_settings)
            self._json(200, {"slug": slug, "enabled": bool(enabled), "ok": True})
        elif self.path.startswith("/api/mic/"):
            slug = self.path.split("/")[-1]
            with _devices_lock:
                md = _mic_devices.get(slug)
            if not md:
                self._json(404, {"error": f"mic '{slug}' not found"})
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            vol = body.get("volume")
            if vol is None:
                self._json(400, {"error": "provide volume 0-500"})
                return
            vol = max(0, min(500, int(vol)))
            enabled = vol > 0
            mic_settings = _load_mic_settings()
            mic_settings[slug] = {"volume": vol, "enabled": enabled}
            _save_mic_settings(mic_settings)
            self._json(200, {"slug": slug, "volume": vol, "enabled": enabled, "ok": True})
        elif self.path == "/api/mic-test":
            global _mic_test_proc
            # Check if a test is already running
            if _mic_test_proc is not None and _mic_test_proc.poll() is None:
                self._json(409, {"error": "test already in progress"})
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            seconds = max(1, min(10, int(body.get("seconds", 3))))
            _mic_test_proc = subprocess.Popen(
                [sys.executable, MIC_TEST_SCRIPT, str(seconds)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._json(200, {"ok": True, "seconds": seconds, "pid": _mic_test_proc.pid})
        else:
            self._json(404, {"error": "not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


if __name__ == "__main__":
    # Initial device discovery
    _devices = _discover_output_devices()
    _mic_devices = _discover_mic_devices()

    print("Discovered output devices:")
    for slug, d in _devices.items():
        dev = _resolve_device(slug)
        if dev:
            vol = _get_volume(dev["device_id"], dev["channels"])
            print(f"  {d['icon']} {d['display']}: ID={dev['device_id']} channels={dev['channels']} volume={vol}%")
        else:
            print(f"  {d['icon']} {d['display']}: not connected (will retry on each request)")

    print("Discovered mic devices:")
    for slug, md in _mic_devices.items():
        print(f"  {md['icon']} {md['display']}")

    # Start background device refresh thread
    refresh_thread = threading.Thread(target=_refresh_devices, name="device-refresh", daemon=True)
    refresh_thread.start()
    print("Started device refresh thread (10s interval)")

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"\nVolume control at http://127.0.0.1:{PORT}")
    print("Devices auto-discovered and refreshed every 10s\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
