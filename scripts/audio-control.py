#!/usr/bin/env python3
"""Unified audio control API — single endpoint for AI assistant to manage all audio state.

Consolidates speaker volume/routing, mic control, and scene management behind one API
on port 8789. Reads/writes the same JSON config files that audio-forward (Rust) and
mic-forward (Python) already watch.

Endpoints:
  GET  /health
  GET  /api/state              — full audio state snapshot
  GET  /api/devices            — list all output devices with state
  POST /api/devices/{slug}/volume  — set volume (0-100)
  POST /api/devices/{slug}/mute    — mute/unmute
  POST /api/devices/{slug}/enable  — enable/disable routing
  GET  /api/mics               — list all microphones with state
  POST /api/mics/{slug}/gain   — set mic gain
  POST /api/mics/{slug}/enable — enable/disable mic
  GET  /api/scenes             — list saved scenes
  POST /api/scenes             — create/update a scene
  POST /api/scenes/{name}/apply — apply a scene
  DELETE /api/scenes/{name}    — delete a scene
  GET  /api/apps               — list apps currently using CaptureAudio
"""

import json
import os
import subprocess
import tempfile

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --- Paths ---

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
SPEAKER_SETTINGS_PATH = os.path.join(_PROJECT_ROOT, "data", "speaker-settings.json")
MIC_SETTINGS_PATH = os.path.join(_PROJECT_ROOT, "data", "mic-settings.json")
SCENES_PATH = os.path.join(_PROJECT_ROOT, "data", "audio-scenes.json")
CLIENTS_PATH = "/tmp/capture-audio-clients.json"

VOLUME_CONTROL_BASE = "http://127.0.0.1:8788"

# --- JSON file helpers ---


def _load_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_speaker_settings() -> dict:
    return _load_json(SPEAKER_SETTINGS_PATH)


def _save_speaker_settings(data: dict) -> None:
    _save_json(SPEAKER_SETTINGS_PATH, data)


def _load_mic_settings() -> dict:
    raw = _load_json(MIC_SETTINGS_PATH)
    return {slug: _normalize_mic_setting(slug, value) for slug, value in raw.items()}


def _save_mic_settings(data: dict) -> None:
    normalized = {slug: _normalize_mic_setting(slug, value) for slug, value in data.items()}
    _save_json(MIC_SETTINGS_PATH, normalized)


def _load_scenes() -> dict:
    return _load_json(SCENES_PATH)


def _save_scenes(data: dict) -> None:
    _save_json(SCENES_PATH, data)


def _slugify(name: str) -> str:
    return name.lower().replace(" ", "-")


PROTECTED_MIC_SLUGS: set[str] = set()


def _normalize_mic_setting(slug: str, data: dict | None = None) -> dict:
    normalized = dict(data or {})
    if slug in PROTECTED_MIC_SLUGS:
        current_gain = normalized.get("volume", 0)
        if isinstance(current_gain, (int, float)) and current_gain > 0:
            normalized.setdefault("pre_disable_gain", int(current_gain))
        normalized["volume"] = 0
        normalized["enabled"] = False
        return normalized
    normalized["volume"] = int(normalized.get("volume", 100))
    normalized["enabled"] = bool(normalized.get("enabled", True))
    return normalized


# --- Volume control proxy (HAL volume on :8788) ---


async def _proxy_hal_volume(slug: str, volume: int) -> bool:
    """Proxy HAL volume change to the volume-control service on :8788."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.post(
                f"{VOLUME_CONTROL_BASE}/api/volume/{slug}",
                json={"volume": volume},
            )
            return r.status_code == 200
    except httpx.HTTPError:
        return False


async def _get_hal_devices() -> dict:
    """Get device list from volume-control :8788."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{VOLUME_CONTROL_BASE}/api/devices")
            if r.status_code == 200:
                return r.json()
    except httpx.HTTPError:
        pass
    return {}


# --- FastAPI app ---

app = FastAPI(title="Audio Control API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "audio-control"}


@app.get("/api/state")
async def get_state():
    """Full audio state snapshot — the key AI endpoint."""
    speaker_settings = _load_speaker_settings()
    mic_settings = _load_mic_settings()
    scenes = _load_scenes()

    # Get live HAL device info from volume-control
    hal_devices = await _get_hal_devices()

    devices = {}
    # Merge HAL device info with speaker settings
    all_slugs = set(hal_devices.keys()) | set(speaker_settings.keys())
    for slug in sorted(all_slugs):
        hal = hal_devices.get(slug, {})
        spk = speaker_settings.get(slug, {})
        display = hal.get("display", slug.replace("-", " ").title())
        connected = "error" not in hal and bool(hal)
        # Software volume from settings (what audio-forward uses)
        sw_volume = spk.get("volume", 100)
        enabled = spk.get("enabled", True)
        muted = hal.get("muted", False)
        devices[slug] = {
            "display": display,
            "connected": connected,
            "volume": sw_volume,
            "muted": muted,
            "routing_enabled": enabled,
        }

    mics = {}
    for slug, s in mic_settings.items():
        display = slug.replace("-", " ").title()
        mics[slug] = {
            "display": display,
            "enabled": s.get("enabled", True),
            "gain": s.get("volume", 100),
        }

    # Determine active scene by checking which scene matches current state
    active_scene = _detect_active_scene(speaker_settings, mic_settings, scenes)

    return {
        "devices": devices,
        "mics": mics,
        "active_scene": active_scene,
        "scenes": sorted(scenes.keys()),
    }


def _detect_active_scene(
    speaker_settings: dict, mic_settings: dict, scenes: dict
) -> str | None:
    """Check if current settings match any saved scene."""
    for name, scene in scenes.items():
        match = True
        scene_devices = scene.get("devices", {})
        for slug, sd in scene_devices.items():
            current = speaker_settings.get(slug, {})
            if sd.get("volume") is not None and current.get("volume") != sd["volume"]:
                match = False
                break
            if sd.get("enabled") is not None and current.get("enabled", True) != sd["enabled"]:
                match = False
                break
        if not match:
            continue
        scene_mics = scene.get("mics", {})
        for slug, sm in scene_mics.items():
            current = mic_settings.get(slug, {})
            if sm.get("gain") is not None and current.get("volume") != sm["gain"]:
                match = False
                break
            if sm.get("enabled") is not None and current.get("enabled", True) != sm["enabled"]:
                match = False
                break
        if match:
            return name
    return None


# --- Device endpoints ---


@app.get("/api/devices")
async def list_devices():
    """List all output devices with state."""
    speaker_settings = _load_speaker_settings()
    hal_devices = await _get_hal_devices()

    devices = {}
    all_slugs = set(hal_devices.keys()) | set(speaker_settings.keys())
    for slug in sorted(all_slugs):
        hal = hal_devices.get(slug, {})
        spk = speaker_settings.get(slug, {})
        display = hal.get("display", slug.replace("-", " ").title())
        connected = "error" not in hal and bool(hal)
        devices[slug] = {
            "slug": slug,
            "display": display,
            "connected": connected,
            "volume": spk.get("volume", 100),
            "muted": hal.get("muted", False),
            "routing_enabled": spk.get("enabled", True),
        }
    return devices


class VolumeRequest(BaseModel):
    volume: int


@app.post("/api/devices/{slug}/volume")
async def set_device_volume(slug: str, req: VolumeRequest):
    """Set device volume (0-100). Updates speaker-settings.json and proxies to HAL."""
    volume = max(0, min(100, req.volume))
    spk = _load_speaker_settings()
    if slug not in spk:
        spk[slug] = {"enabled": True}
    spk[slug]["volume"] = volume
    _save_speaker_settings(spk)

    # Also proxy to HAL volume control
    await _proxy_hal_volume(slug, volume)

    return {"slug": slug, "volume": volume, "ok": True}


class MuteRequest(BaseModel):
    muted: bool


@app.post("/api/devices/{slug}/mute")
async def set_device_mute(slug: str, req: MuteRequest):
    """Mute/unmute a device. Saves pre-mute volume for restore."""
    spk = _load_speaker_settings()
    if slug not in spk:
        spk[slug] = {"enabled": True}
    if req.muted:
        # Save current volume before muting so we can restore it
        current_vol = spk[slug].get("volume", 100)
        if current_vol > 0:
            spk[slug]["pre_mute_volume"] = current_vol
        spk[slug]["volume"] = 0
    else:
        # Restore the pre-mute volume (only if we have a saved value)
        restore_vol = spk[slug].pop("pre_mute_volume", None)
        if restore_vol is not None:
            spk[slug]["volume"] = restore_vol
    _save_speaker_settings(spk)

    vol = spk[slug].get("volume", 0)
    await _proxy_hal_volume(slug, vol)

    return {"slug": slug, "muted": req.muted, "volume": vol, "ok": True}


class EnableRequest(BaseModel):
    enabled: bool


@app.post("/api/devices/{slug}/enable")
async def set_device_enable(slug: str, req: EnableRequest):
    """Enable/disable routing for a device. Audio-forward reads this."""
    spk = _load_speaker_settings()
    if slug not in spk:
        spk[slug] = {}
    spk[slug]["enabled"] = req.enabled
    _save_speaker_settings(spk)
    return {"slug": slug, "routing_enabled": req.enabled, "ok": True}


# --- Mic endpoints ---


@app.get("/api/mics")
async def list_mics():
    """List all microphones with state."""
    mic_settings = _load_mic_settings()
    mics = {}
    for slug, s in mic_settings.items():
        display = slug.replace("-", " ").title()
        mics[slug] = {
            "slug": slug,
            "display": display,
            "gain": s.get("volume", 100),
            "enabled": s.get("enabled", True),
        }
    return mics


class GainRequest(BaseModel):
    gain: int


@app.post("/api/mics/{slug}/gain")
async def set_mic_gain(slug: str, req: GainRequest):
    """Set mic gain (0-500)."""
    gain = max(0, min(500, req.gain))
    mic = _load_mic_settings()
    if slug not in mic:
        raise HTTPException(status_code=404, detail=f"mic '{slug}' not found")
    mic[slug]["volume"] = gain
    mic[slug]["enabled"] = gain > 0
    mic[slug] = _normalize_mic_setting(slug, mic[slug])
    _save_mic_settings(mic)
    return {"slug": slug, "gain": mic[slug]["volume"], "enabled": mic[slug]["enabled"], "ok": True}


class MicEnableRequest(BaseModel):
    enabled: bool


@app.post("/api/mics/{slug}/enable")
async def set_mic_enable(slug: str, req: MicEnableRequest):
    """Enable/disable a microphone. Preserves gain for restore on re-enable."""
    mic = _load_mic_settings()
    if slug not in mic:
        raise HTTPException(status_code=404, detail=f"mic '{slug}' not found")
    mic[slug]["enabled"] = req.enabled
    if not req.enabled:
        # Save current gain before disabling
        current_gain = mic[slug].get("volume", 100)
        if current_gain > 0:
            mic[slug]["pre_disable_gain"] = current_gain
        mic[slug]["volume"] = 0
    else:
        # Restore the pre-disable gain (only if we have a saved value)
        restore_gain = mic[slug].pop("pre_disable_gain", None)
        if restore_gain is not None and mic[slug].get("volume", 0) == 0:
            mic[slug]["volume"] = restore_gain
    mic[slug] = _normalize_mic_setting(slug, mic[slug])
    _save_mic_settings(mic)
    return {"slug": slug, "enabled": mic[slug]["enabled"], "gain": mic[slug].get("volume", 0), "ok": True}


# --- Scene endpoints ---


@app.get("/api/scenes")
async def list_scenes():
    """List all saved scenes."""
    scenes = _load_scenes()
    return {"scenes": list(scenes.keys()), "definitions": scenes}


class SceneDefinition(BaseModel):
    name: str
    devices: dict | None = None
    mics: dict | None = None


@app.post("/api/scenes")
async def create_or_update_scene(scene: SceneDefinition):
    """Create or update a scene definition."""
    scenes = _load_scenes()
    definition: dict = {}
    if scene.devices is not None:
        definition["devices"] = scene.devices
    if scene.mics is not None:
        definition["mics"] = scene.mics
    scenes[scene.name] = definition
    _save_scenes(scenes)
    return {"name": scene.name, "ok": True}


@app.post("/api/scenes/{name}/apply")
async def apply_scene(name: str):
    """Apply a saved scene — bulk update all devices and mics."""
    scenes = _load_scenes()
    if name not in scenes:
        raise HTTPException(status_code=404, detail=f"scene '{name}' not found")

    scene = scenes[name]
    spk = _load_speaker_settings()
    mic = _load_mic_settings()

    # Apply device settings
    for slug, sd in scene.get("devices", {}).items():
        if slug not in spk:
            spk[slug] = {}
        if "volume" in sd:
            spk[slug]["volume"] = sd["volume"]
        if "enabled" in sd:
            spk[slug]["enabled"] = sd["enabled"]
        # Proxy HAL volume
        if "volume" in sd:
            await _proxy_hal_volume(slug, sd["volume"])

    # Apply mic settings
    for slug, sm in scene.get("mics", {}).items():
        if slug not in mic:
            mic[slug] = {}
        if "gain" in sm:
            mic[slug]["volume"] = sm["gain"]
        if "enabled" in sm:
            mic[slug]["enabled"] = sm["enabled"]
            if not sm["enabled"]:
                mic[slug]["volume"] = 0
        mic[slug] = _normalize_mic_setting(slug, mic[slug])

    _save_speaker_settings(spk)
    _save_mic_settings(mic)
    return {"scene": name, "applied": True, "ok": True}


@app.delete("/api/scenes/{name}")
async def delete_scene(name: str):
    """Delete a saved scene."""
    scenes = _load_scenes()
    if name not in scenes:
        raise HTTPException(status_code=404, detail=f"scene '{name}' not found")
    del scenes[name]
    _save_scenes(scenes)
    return {"name": name, "deleted": True, "ok": True}


# --- App tracking (Phase 3b) ---


@app.get("/api/apps")
async def list_apps():
    """List apps currently using CaptureAudio (requires Phase 3a driver changes)."""
    clients = _load_json(CLIENTS_PATH)
    if not clients:
        return []
    if isinstance(clients, list):
        entries = clients
    else:
        return []

    result = []
    for entry in entries:
        pid = entry.get("pid")
        bundle_id = entry.get("bundle_id", "")
        name = entry.get("name", "")
        # Enrich with process name if not already provided
        if pid and not name:
            try:
                out = subprocess.check_output(
                    ["ps", "-p", str(pid), "-o", "comm="],
                    text=True,
                    timeout=1,
                ).strip()
                name = os.path.basename(out) if out else ""
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass
        result.append({"pid": pid, "bundle_id": bundle_id, "name": name})
    return result


# --- Main ---

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8789, log_level="info")
