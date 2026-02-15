#!/usr/bin/env python3
"""Multi-device volume control via CoreAudio HAL API.

Controls volume directly on audio devices without switching
the system output device. No audio glitches.

Runs on port 8788.
"""

import ctypes
import ctypes.util
import json
import struct
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

PORT = 8788

# Devices to control: (slug, display_name, device_name, icon)
DEVICES = [
    ("speakers", "MacBook Pro Speakers", "MacBook Pro Speakers", "🔊"),
    ("headphones", "Bose QC45", "Bose QC45", "🎧"),
]

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


# --- Device registry (names are stable, IDs are looked up on each request) ---

_devices: dict[str, dict] = {}

for slug, display, dev_name, icon in DEVICES:
    _devices[slug] = {
        "slug": slug,
        "display": display,
        "dev_name": dev_name,
        "icon": icon,
    }


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
    display: flex; justify-content: center; align-items: center;
    min-height: 100vh; gap: 20px; flex-wrap: wrap; padding: 20px;
  }
  .card {
    background: #2a2a2a; border-radius: 16px; padding: 28px;
    width: 300px; box-shadow: 0 8px 32px rgba(0,0,0,0.4);
  }
  .card.disconnected { opacity: 0.4; }
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
  }
  .btn-mute:hover { background: #444; }
  .btn-mute.muted { background: #c33; color: #fff; }
  .status { text-align: center; color: #555; font-size: 10px; margin-top: 10px; }
  .keys { text-align: center; color: #444; font-size: 10px; margin-top: 24px; width: 100%; }
  kbd { background: #333; padding: 2px 6px; border-radius: 3px; font-size: 10px; }
</style>
</head>
<body>
<script>
const DEVICES = __DEVICES_JSON__;

function createCard(dev) {
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
    <div class="status" id="status-${dev.slug}">Connecting...</div>
  `;
  document.body.appendChild(card);

  const slider = document.getElementById('slider-' + dev.slug);
  let debounce = null;
  slider.addEventListener('input', () => {
    document.getElementById('vol-' + dev.slug).textContent = slider.value;
    clearTimeout(debounce);
    debounce = setTimeout(() => setVol(dev.slug, parseInt(slider.value)), 80);
  });
}

const state = {};
DEVICES.forEach(d => {
  state[d.slug] = { current: 50, preMute: 50, muted: false };
  createCard(d);
});

// Add keyboard hint
const keys = document.createElement('div');
keys.className = 'keys';
keys.innerHTML = 'Keyboard: <kbd>↑</kbd><kbd>↓</kbd> speakers ±5 &nbsp; <kbd>⇧↑</kbd><kbd>⇧↓</kbd> headphones ±5 &nbsp; <kbd>M</kbd> mute speakers &nbsp; <kbd>⇧M</kbd> mute headphones';
document.body.appendChild(keys);

async function fetchVol(slug) {
  try {
    const r = await fetch('/api/volume/' + slug);
    const d = await r.json();
    const card = document.getElementById('card-' + slug);
    if (d.error) {
      card.classList.add('disconnected');
      document.getElementById('status-' + slug).textContent = d.error;
      return;
    }
    card.classList.remove('disconnected');
    const s = state[slug];
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
    document.getElementById('status-' + slug).textContent = 'Disconnected';
  }
}

async function setVol(slug, v) {
  v = Math.max(0, Math.min(100, Math.round(v)));
  const s = state[slug];
  s.current = v;
  if (v > 0) { s.muted = false; s.preMute = v; }
  document.getElementById('slider-' + slug).value = v;
  document.getElementById('vol-' + slug).textContent = v;
  const muteBtn = document.getElementById('mute-' + slug);
  muteBtn.textContent = s.muted ? 'Unmute' : 'Mute';
  muteBtn.classList.toggle('muted', s.muted);
  document.getElementById('vol-' + slug).classList.toggle('muted', s.muted);
  try {
    await fetch('/api/volume/' + slug, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({volume: v})
    });
  } catch(e) {}
}

function nudge(slug, delta) { setVol(slug, state[slug].current + delta); }

function toggleMute(slug) {
  const s = state[slug];
  if (s.muted) { setVol(slug, s.preMute || 50); }
  else { s.preMute = s.current; setVol(slug, 0); }
}

document.addEventListener('keydown', (e) => {
  const slug = e.shiftKey ? 'headphones' : 'speakers';
  if (!state[slug]) return;
  if (e.key === 'ArrowUp' || e.key === 'ArrowRight') { e.preventDefault(); nudge(slug, 5); }
  if (e.key === 'ArrowDown' || e.key === 'ArrowLeft') { e.preventDefault(); nudge(slug, -5); }
  if (e.key === 'm' || e.key === 'M') { toggleMute(slug); }
});

// Initial fetch + light poll
DEVICES.forEach(d => fetchVol(d.slug));
setInterval(() => DEVICES.forEach(d => fetchVol(d.slug)), 30000);
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
            devices_json = json.dumps([
                {"slug": d["slug"], "display": d["display"], "icon": d["icon"]}
                for d in _devices.values()
            ])
            html = HTML.replace("__DEVICES_JSON__", devices_json)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())
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
            result = {}
            for slug in _devices:
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
                    d = _devices[slug]
                    result[slug] = {
                        "slug": d["slug"],
                        "display": d["display"],
                        "icon": d["icon"],
                        "volume": None,
                        "muted": None,
                        "error": "not connected",
                    }
            self._json(200, result)
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
        else:
            self._json(404, {"error": "not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


if __name__ == "__main__":
    print("Registered devices:")
    for slug, d in _devices.items():
        dev = _resolve_device(slug)
        if dev:
            vol = _get_volume(dev["device_id"], dev["channels"])
            print(f"  {d['icon']} {d['display']}: ID={dev['device_id']} channels={dev['channels']} volume={vol}%")
        else:
            print(f"  {d['icon']} {d['display']}: not connected (will retry on each request)")
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"\nVolume control at http://127.0.0.1:{PORT}")
    print("Keyboard: ↑↓ = speakers, Shift+↑↓ = headphones, M/Shift+M = mute\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
