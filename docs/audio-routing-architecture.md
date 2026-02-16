# Audio Routing Architecture

## Overview

The personal assistant uses two virtual audio drivers as a hub for all audio routing. This eliminates device-switching glitches and enables multiple services to share audio simultaneously.

```
Physical mics ──> mic-forward.py ──> CaptureMic 2ch (universal input)
  MacBook Pro Mic (always on)           |
  Bose QC45 mic (DISABLED*)            |---> Discord bot
  [auto-discovered inputs]              |---> Voice chat service
                                        |---> audio-assist transcription
                                        '---> Any app using system default mic

All apps ──> CaptureAudio 2ch (universal output) ──> audio-forward.py ──> Physical outputs
  Voice chat TTS                                       MacBook Pro Speakers
  Discord bot TTS                                      Bose QC45 (A2DP stereo)
  System audio                                         [auto-discovered outputs]
```

*\*Bose QC45 mic MUST stay disabled. Opening it forces Bluetooth HFP mode (16kHz mono), which degrades output quality from 44.1kHz stereo to 16kHz mono.*

## Virtual Drivers

| Driver | Bundle ID | Role |
|--------|-----------|------|
| CaptureAudio 2ch | `com.tdeshane.CaptureAudio2ch` | System default output. Apps write here; audio-forward fans out to physical speakers. audio-assist captures at 16kHz for transcription. |
| CaptureMic 2ch | `com.tdeshane.CaptureMic2ch` | Universal mic input. mic-forward mixes all physical mics here. Apps read it as their mic source. |

Both are HAL AudioServerPlugins in `/Library/Audio/Plug-Ins/HAL/` with lock-free ring buffers supporting multiple simultaneous readers at different sample rates.

## Services

| Service | Port | Process | Purpose |
|---------|------|---------|---------|
| audio-assist | 8787 | `.venv/bin/audio-assist` | Always-on capture + Whisper transcription |
| volume-control | 8788 | `scripts/volume-control.py` | CoreAudio volume/mute + device discovery |
| discord-bot | 8796 | `discord_bot.main:app` | Discord voice AI (STT/LLM/TTS) |
| voice-chat | 8797 | `voice_chat.service` | Interactive voice AI |
| mic-forward | - | `scripts/mic-forward.py` | Physical mics -> CaptureMic 2ch |
| audio-forward | - | `scripts/audio-forward.py` | CaptureAudio 2ch -> physical speakers |

## Tauri App Integration

The Tauri app (system tray, `app/`) controls routing via these APIs:

| API | Endpoint | Data File |
|-----|----------|-----------|
| Speaker volume/mute | `POST /api/volume/{slug}` | CoreAudio direct |
| Speaker routing on/off | `POST /api/speaker/{slug}/enable` | `data/speaker-settings.json` |
| Mic gain (0-500%) | `POST /api/mic/{slug}` | `data/mic-settings.json` |
| Mic live levels | `GET /api/mic-levels` | `data/mic-levels.json` |
| Device discovery | `GET /api/devices`, `GET /api/mics` | Auto-discovered |

## Persistent State Files

| File | Written By | Read By | Purpose |
|------|-----------|---------|---------|
| `data/mic-settings.json` | volume-control, mic-forward | mic-forward, volume-control, Tauri | Per-mic gain and enabled state |
| `data/mic-levels.json` | mic-forward (every 200ms) | volume-control, Tauri | Live dBFS level meters |
| `data/speaker-settings.json` | volume-control | audio-forward, volume-control, Tauri | Per-speaker routing enable/disable |

## Critical Constraint: Bluetooth HFP vs A2DP

Bluetooth headsets (Bose QC45) have two mutually exclusive profiles:

| Profile | Output Quality | Mic Available | Triggered By |
|---------|---------------|---------------|-------------|
| A2DP | 44.1kHz stereo (good) | No | Default when no mic is open |
| HFP | 16kHz mono (muddy) | Yes | ANY process opening the mic input |

**Rule: Never open the Bose QC45 mic input.** This means:
- `data/mic-settings.json`: Bose QC45 `enabled: false`, `volume: 0`
- `audio-assist` bose-mic capture: will retry silently but fail gracefully when A2DP (no mic available)
- C binary `driver/build/mic-forward`: defaults changed to MacBook Pro Microphone only

If Bose audio sounds muddy, check `blueutil` or System Settings > Bluetooth. A disconnect/reconnect cycle resets to A2DP:
```bash
blueutil --disconnect AC:BF:71:69:17:48 && sleep 3 && blueutil --connect AC:BF:71:69:17:48
```

## Service Startup

`scripts/audio-assist-service.sh start` launches:
1. audio-assist (port 8787)
2. audio-forward (CaptureAudio -> speakers)
3. mic-forward (physical mics -> CaptureMic, Python multi-mic mixer)

`scripts/volume-control-service.sh start` launches:
1. volume-control (port 8788)

Discord bot and voice-chat are started separately.

## Health Check

Run `/check-audio-routing` in Claude Code or `scripts/check-audio-routing.sh` to verify:
- All services are running on expected ports
- Virtual drivers are loaded
- Bose is in A2DP mode (not HFP)
- Bose mic is disabled in settings
- mic-levels.json is fresh (mic-forward writing)
- audio-forward is routing to expected outputs
- Tauri app APIs are responding
