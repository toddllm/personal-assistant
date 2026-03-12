# Zoom Chrome Bot — Status Report

**Date:** 2026-02-15
**Status:** Working — bidirectional voice AI in Zoom meetings

## Summary

The Chrome-based Zoom bot joins meetings via Playwright and runs a full voice AI pipeline:
Deepgram STT → Groq LLM → ElevenLabs TTS. The bot can hear participants and respond
with synthesized speech. Two successful live test sessions were completed today.

---

## Pipeline Steps — Working vs Issues

### 1. VM & Service Infrastructure
**Status: WORKING**

- OrbStack amd64 Ubuntu VM (`zoom-bot`) running
- FastAPI service on port 8795 (`/health`, `/status`, `/join`, `/leave`)
- Systemd service for auto-restart
- PulseAudio configured with `virtual_output` sink

### 2. Chrome Launch & Meeting Join
**Status: WORKING**

- Playwright launches Chromium (non-headless via Xvfb)
- Navigates to `app.zoom.us/wc/join/{meeting_id}`
- Fills passcode, name ("AI Assistant"), clicks Join
- Detects in-meeting indicators (`.meeting-app`, `#foot-bar`, etc.)
- Clicks "Join Audio by Computer" and consent buttons
- Join time: ~15-20 seconds from API call to in-meeting state

**Chrome flags:**
- `--use-fake-device-for-media-stream` — bypasses Chrome APM (proven working)
- `--use-file-for-fake-audio-capture=/tmp/bot-primer.wav` — 120s primer tone to start WebRTC
- `--use-fake-ui-for-media-stream` — auto-grant mic/camera permissions

### 3. Inbound Audio (Meeting → STT)
**Status: WORKING**

- `parecord` captures from `virtual_output.monitor` at 16kHz mono
- Audio upsampled 16kHz → 32kHz (sample duplication) for pipeline compatibility
- Fed to `AudioPipeline.feed_audio()`
- Deepgram WebSocket STT (nova-2 model) transcribes in real-time
- Transcripts logged and processed

**Verified:** Deepgram transcribed 19 user utterances across two sessions.

### 4. LLM Response Generation
**Status: WORKING**

- Groq API with `llama-3.3-70b-versatile`
- Conversation history maintained (last 20 turns)
- Response time: ~0.5-1.0 seconds
- Responses are concise (max 200 tokens)

**Verified:** 19 contextually appropriate responses generated.

### 5. TTS Audio Generation (ElevenLabs)
**Status: WORKING (after critical fix)**

- ElevenLabs API with `eleven_turbo_v2_5` model, "Sarah" voice
- **Critical fix applied:** `output_format=pcm_22050` MUST be a query parameter, NOT in the JSON body. When in the body, ElevenLabs ignores it and returns MP3 — this was the root cause of all "static" audio in previous sessions.
- Returns raw s16le mono PCM at 22050Hz
- Typical response: 2-9 seconds of audio (150KB-880KB)

**Verified offline:** WAV files at 22050Hz, 32000Hz, and 48000Hz all play clean speech.

### 6. Audio Resampling Chain
**Status: WORKING**

- `audio_pipeline.py`: ElevenLabs 22050Hz → `resample_to_32k()` → 32kHz (linear interpolation)
- `chrome_bot.py`: 32kHz → `_resample_32k_to_48k()` → 48kHz (linear interpolation, 3:2 ratio)
- Duration preserved at each stage (verified: 4.78s → 4.78s → 4.78s)
- Signal quality preserved (peak ~29k, RMS ~5.7k at all stages)

### 7. WebSocket Audio Delivery (Python → Browser)
**Status: WORKING**

- `AudioWebSocketServer` on port 8791
- Sends 48kHz s16le mono PCM in 20ms chunks (1920 bytes each)
- Browser connects via init script (`window.__botStartAudioWS`)
- Ring buffer (10 seconds / 480,000 samples) smooths delivery
- ScriptProcessorNode (4096 samples/callback) continuously feeds MediaStreamDestination

**Session 2 stats:** 5,366,802 samples received, 5,597 chunks played.

### 8. WebRTC Track Replacement
**Status: PARTIALLY WORKING — needs improvement**

After Chrome joins the meeting, we scan RTCPeerConnections and replace the audio sender's
track with our Web Audio MediaStreamDestination. This bypasses Chrome's fake device audio
(primer tone) with our TTS speech.

#### Session 1 (first join):
- Track replacement on **PC#3** (the correct transmitting PC, `bytesSent=50932`)
- `bytesSent` continued growing after replacement (55K → 206K)
- `totalAudioEnergy=4.33`, `audioLevel=0.45` — strong signal
- **User heard bot immediately after replacement**

#### Session 2 (rejoin):
- Phase 1 waited 20s for `audioSenderCount > 0` (never fires — Zoom uses `addTransceiver`, not `addTrack`)
- Track replacement on **PC#2** via Fallback1 (`bytesSent=0`, live audio track)
- `bytesSent` stayed at **0** the entire session (wrong PC!)
- `totalAudioEnergy=23.87` — audio was being fed into MediaStreamDestination
- **User heard bot but with significant delay (~60 seconds)**

**Key issues:**
1. **Phase 1 always times out (20s wasted):** Zoom uses `addTransceiver()` not `addTrack()`, so our `addTrack` hook never fires. `audioSenderCount` is always 0.
2. **Fallback picks wrong PC:** Fallback1 picks the first stable PC with an audio sender, which may not be the actual transmitting PC. In session 2, it picked PC#2 (bytesSent=0) instead of the transmitting PC.
3. **Unclear how audio reached user in session 2:** With bytesSent=0 on the replaced PC, the audio shouldn't have transmitted. Possible explanations: (a) Zoom's internal audio routing picked it up from the AudioContext, (b) a different PC eventually started transmitting our audio, or (c) the primer WAV on the actual transmitting PC was mixed with our MediaStreamDestination output.

### 9. Process Cleanup (Leave)
**Status: WORKING**

- `main.py` sends SIGTERM to main process (clean `browser.close()`)
- Falls back to `killpg(SIGKILL)` after 15s timeout
- `_kill_orphan_processes()` runs `pkill -f` for chromium/Xvfb
- Process group isolation via `start_new_session=True`

**Verified:** No ghost Chrome/Xvfb processes after leave in session 2.

### 10. Local Transcript Capture
**Status: WORKING**

- Local `audio-assist` service on Mac captures meeting audio via Mac audio devices
- Both user speech (attributed to "Todd Deshane") and bot speech (unattributed) captured
- Stored in SQLite database at `data/audio_assist.db`
- Test tone captured as "Beeeeeee..."

---

## Known Issues (Priority Order)

### P0: Track Replacement Picks Wrong PC
**Impact:** Bot audio may not transmit, or transmits with long delay.
**Root cause:** Fallback1 picks first stable PC with audio sender, not the actual transmitting PC. In session 1 the correct PC had `bytesSent=50932`; in session 2 no PC had `bytesSent > 0` at scan time.
**Fix:** Wait for `bytesSent > 0` on any PC before attempting replacement, rather than falling back to any audio sender. The primer WAV should ensure at least one PC transmits — we just need to wait long enough.

### P1: Phase 1 Wait Wastes 20 Seconds
**Impact:** Adds 20s to join-to-audio time.
**Root cause:** `addTrack` hook never fires because Zoom uses `addTransceiver`. Phase 1 always runs all 10 iterations (2s each).
**Fix:** Remove Phase 1 entirely, or replace with `addTransceiver` hook.

### P2: Bot Interrupts Itself / Doesn't Wait for User to Finish
**Impact:** Bot responds to partial sentences. Example: "Okay. It's" → bot responds, then "was a huge delay..." → bot responds again.
**Root cause:** Deepgram `endpointing=300` (300ms silence triggers final transcript). Short pauses mid-sentence trigger premature responses.
**Fix:** Increase endpointing to 500-800ms, or add a debounce delay in `_handle_deepgram_message` to wait for more text.

### P3: WebSocket 426 Log Noise
**Impact:** Clutters logs, no functional impact.
**Root cause:** Chrome's service worker probes the WS port with plain HTTP.
**Fix:** Add `process_request` handler to websockets server to silently reject non-WS requests.

### P4: Bot Responses Too Long
**Impact:** Bot talks for 8-13 seconds, leaving no room for conversation.
**Root cause:** LLM `max_tokens=200` allows long responses. System prompt says "1-2 sentences" but LLM doesn't always comply.
**Fix:** Reduce `max_tokens` to 80-100, or add stronger length constraints to system prompt.

---

## Architecture

```
Mac (Apple Silicon)
├── audio-assist service (local transcription via Whisper)
│   └── SQLite DB: data/audio_assist.db
├── Zoom desktop client (user's audio)
└── OrbStack VM (zoom-bot, amd64 Ubuntu 22.04)
    ├── FastAPI service (port 8795)
    │   └── _BotManager → subprocess → chrome_bot.py
    ├── chrome_bot.py
    │   ├── Playwright → Chromium (via Xvfb :99)
    │   ├── PulseAudio: virtual_output sink
    │   ├── parecord: meeting audio capture → pipeline
    │   └── AudioWebSocketServer (port 8791) → browser JS
    └── audio_pipeline.py
        ├── Deepgram WebSocket STT (nova-2)
        ├── Groq LLM (llama-3.3-70b)
        └── ElevenLabs TTS (eleven_turbo_v2_5, pcm_22050)
```

### Audio Flow
```
Inbound (meeting → bot):
  Participants speak → Chrome WebRTC → virtual_output (PA sink)
    → parecord (16kHz) → upsample to 32kHz → AudioPipeline.feed_audio()
    → Deepgram STT → Groq LLM

Outbound (bot → meeting):
  ElevenLabs TTS (22050Hz PCM) → resample to 32kHz (audio_pipeline)
    → resample to 48kHz (chrome_bot) → WebSocket (port 8791)
    → Browser JS: Int16→Float32 → ring buffer → ScriptProcessorNode
    → MediaStreamDestination → replaceTrack on WebRTC sender
    → participants hear AI speech
```

---

## Session Logs

### Session 1 (15:02-15:07) — First successful voice conversation
- Track replaced on correct PC (PC#3, bytesSent=50932)
- 14 conversation turns
- User confirmed: "Oh, there you are. I hear you now."
- Clean leave, no ghost processes

### Session 2 (15:10-15:15) — Rejoin test
- Track replaced on wrong PC (PC#2, bytesSent=0)
- ~60s delay before user heard bot
- 19 conversation turns once working
- User confirmed: "it is working finally now"
- Local audio_assist captured both sides of conversation

---

## Test Results Summary

| Component | Status | Notes |
|-----------|--------|-------|
| VM/Service infrastructure | PASS | FastAPI on 8795, systemd service |
| Chrome meeting join | PASS | 15-20s join time |
| PulseAudio routing | PASS | virtual_output for STT capture |
| Deepgram STT | PASS | Real-time transcription, nova-2 |
| Groq LLM | PASS | ~1s response time |
| ElevenLabs TTS | PASS | PCM via query param fix |
| Resampling (22k→32k→48k) | PASS | Duration & quality preserved |
| WebSocket audio delivery | PASS | Chunked, ring buffer, continuous |
| Track replacement (session 1) | PASS | Correct PC, immediate audio |
| Track replacement (session 2) | PARTIAL | Wrong PC, 60s delay |
| Process cleanup | PASS | No ghost processes |
| Local transcript capture | PASS | Both sides in audio_assist DB |

---

## Next Steps

1. **Fix track replacement reliability** — wait for bytesSent > 0 instead of fallback to any PC
2. **Remove Phase 1 wait** — eliminate the 20s `addTrack` detection that never works
3. **Tune conversation flow** — increase endpointing, reduce max_tokens
4. **Suppress WebSocket 426 log noise**
