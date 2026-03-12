# ADR-0002: Audio Capture Architecture

## Status
Accepted

## Date
2026-02-14

## Context
The personal assistant needs to capture audio from multiple sources for transcription and context. The system runs on a MacBook Pro and must handle varying hardware setups: sometimes the user wears Bluetooth headphones (Bose QC45), sometimes they use the built-in speakers, and sometimes external speakers. The capture strategy must work in all cases without manual reconfiguration.

### Failed approaches
- **Multi-Output Device (macOS)**: Fatally buggy. Sample rate drifts to 16 kHz, audio goes silent, routing breaks unpredictably after sleep/wake or Bluetooth reconnect. Rejected.
- **Aggregate Device (macOS)**: Same underlying CoreAudio clock-sync issues as Multi-Output. Rejected.

## Decision

### Three capture sources — full context collection

The assistant needs full context of everything happening. Every source is always on.

| Source ID    | Physical Device            | Sample Rate | Purpose |
|-------------|----------------------------|-------------|---------|
| `desk-mic`  | MacBook Pro Microphone     | 16 kHz mono | Always-on ambient capture. Picks up everything said out loud in the room. Reliable because it's built-in hardware that never disconnects. |
| `app-audio` | CaptureAudio 2ch (virtual) | 16 kHz stereo (transcription) / 48 kHz stereo (forwarding) | Captures what apps output (Zoom, YouTube, Spotify, browser, etc.). Works regardless of whether the user is on speakers or headphones because it intercepts at the application output level, before the physical device. |
| `bose-mic`  | Bose QC45 (Bluetooth)      | 16 kHz mono | Captures the user's voice when wearing headphones. Especially useful when away from the desk or in situations where the built-in mic can't hear clearly. Silently retries when headphones aren't connected. |

### CaptureAudio virtual driver as system default output

CaptureAudio 2ch is set as the **macOS system default output device**. All apps automatically route their audio through it. The driver's ring buffer is lock-free and supports multiple simultaneous readers.

### Audio forwarding daemon

Since CaptureAudio is the default output, a **forwarding daemon** (`scripts/audio-forward.py`) reads the CaptureAudio input at 48 kHz and plays it through the real output device so the user actually hears the audio.

```
Apps (Zoom, YouTube, etc.)
  |  (default output)
CaptureAudio 2ch  (virtual driver, ring buffer loopback)
  |  input side (multiple readers)
  |-- audio-assist: reads at 16 kHz for transcription
  \-- audio-forward.py: reads at 48 kHz, writes to real speakers/headphones
                           |
                     Bose QC45 / MacBook Pro Speakers
```

### Target device flexibility

The forwarding daemon forwards to **all configured target devices simultaneously**, one thread per target. Targets are configured as a comma-separated list via `AUDIO_ASSIST_AUDIO_FORWARD_TARGET_DEVICES`. The default is `"MacBook Pro Speakers,Bose QC45"`.

Each target thread adapts to the device's actual capabilities:
- MacBook Pro Speakers: 2ch stereo @ 48 kHz (full quality)
- Bose QC45 (Bluetooth): 1ch mono @ 16 kHz (stereo->mono down-mix via channel averaging)

If a target device is not connected, its thread retries every 5 seconds until it appears. The user controls what they actually hear via the volume control service (mute speakers when on headphones, etc.).

**Key principle**: The *capture* side never changes. `desk-mic` and `app-audio` always work the same way regardless of what output device is in use. Only the *forwarding* targets are affected.

### Why all three sources, always on

**desk-mic (MacBook Pro Microphone)**:
- Always physically present (built-in, never disconnects)
- Captures all out-loud conversation (meetings, phone calls on speaker, thinking out loud)
- Independent of what apps are doing
- The reliable "catch everything" baseline
- Does NOT capture app audio when headphones are in use (sound doesn't reach the mic)

**app-audio (CaptureAudio 2ch)**:
- Intercepts at the app output level, before any physical device
- Captures regardless of whether user is on speakers or headphones
- Zoom calls, YouTube, Spotify, browser audio — always captured
- No config change needed when switching output devices

**bose-mic (Bose QC45)**:
- Captures the user's voice directly from the headset microphone
- Works when away from desk or when built-in mic can't hear clearly
- Silently retries every 90s when headphones aren't connected (no errors, no noise)
- When headphones are on, provides better voice capture than the desk mic

**Design principle**: Collect everything, always. The assistant needs full context to be useful. Sources that aren't connected simply retry silently. No manual switching, no configuration changes day-to-day.

## Consequences

### Positive
- No dependency on buggy macOS Multi-Output or Aggregate devices
- Capture works identically regardless of output device (speakers, headphones, or none)
- desk-mic provides a reliable always-on baseline for ambient audio
- app-audio captures all application output without hardware dependencies
- Clean separation: capture config never changes, only forwarding target changes

### Negative
- Requires the CaptureAudio virtual driver to be installed and loaded
- Forwarding daemon adds a process and ~10 ms of audio latency
- Switching output devices (speakers <-> headphones) requires changing the forwarding target (manual today, could be automated later)

## Configuration Reference

All settings use the `AUDIO_ASSIST_` env var prefix:

```bash
# Capture sources (these should rarely change)
AUDIO_ASSIST_CAPTURE_AUTOSTART_MIC_DEVICE="MacBook Pro Microphone"
AUDIO_ASSIST_CAPTURE_AUTOSTART_APP_AUDIO_DEVICE="CaptureAudio 2ch"
AUDIO_ASSIST_CAPTURE_AUTOSTART_BOSE_MIC_DEVICE="Bose QC45"
AUDIO_ASSIST_CAPTURE_AUTOSTART_BOSE_MIC_CHANNELS=1

# Forwarding (change target as needed)
AUDIO_ASSIST_AUDIO_FORWARD_ENABLED=true
AUDIO_ASSIST_AUDIO_FORWARD_SOURCE_DEVICE="CaptureAudio 2ch"
AUDIO_ASSIST_AUDIO_FORWARD_TARGET_DEVICES="MacBook Pro Speakers,Bose QC45"
AUDIO_ASSIST_AUDIO_FORWARD_SAMPLE_RATE=48000
```

## Future Improvements
- Auto-detect new output devices via CoreAudio device connect/disconnect events (add new forwarding threads dynamically)
- Health monitoring endpoint on the forwarding daemon for the service dashboard
