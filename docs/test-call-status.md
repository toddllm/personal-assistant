# Test Call Procedures & Status

## Quick Reference

### Your Phone
- **Number**: +1 518 386 8048

### Make a Test Call (AI calls your phone)
```bash
# Start mic-forward (if not already running via audio-assist-service.sh)
driver/build/mic-forward 2>>data/logs/mic-forward.log &

# AI calls your phone (Vapi outbound, max 3 minutes)
.venv/bin/python3 scripts/ai-call.py --phone "+15183868048"

# With custom prompt
.venv/bin/python3 scripts/ai-call.py --phone "+15183868048" --prompt "You are a pirate"

# Shorter call (60 seconds)
.venv/bin/python3 scripts/ai-call.py --phone "+15183868048" --max-duration 60
```

### TTS Pipeline Test (scripted message, no AI)
```bash
.venv/bin/python3 scripts/test-call.py --phone "+15183868048" --message "Testing one two three"
```

### AI into Zoom Meeting
```bash
.venv/bin/python3 scripts/ai-call.py --zoom-number "+16465588656" \
    --meeting-id "1234567890" --passcode "123456"
```

## Audio Pipeline Architecture

```
Physical Mic (Bose QC45 @ 16kHz or MacBook Pro Mic @ 48kHz)
    |
    v
mic-forward (C daemon, driver/build/mic-forward)
  - AUHAL input callback -> resample -> upmix -> ring buffer -> AUHAL output callback
  - Latency: 13-21ms steady state
  - CPU: 0.0%
    |
    v
CaptureMic 2ch (virtual device @ 48kHz stereo)
    |
    v
Browser / Apps read from CaptureMic as mic input
```

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/ai-call.py` | Vapi AI calls a phone or joins Zoom (interactive conversation) |
| `scripts/test-call.py` | Twilio TTS message or Vapi bridge (pipeline smoke test) |
| `scripts/audio-assist-service.sh` | Manages all daemons (start/stop/restart/status) |

## Credentials

Stored in `~/mark-sebast/apps/phone-agent/.env`:
- `VAPI_API_KEY` — Vapi API key
- `VAPI_PHONE_NUMBER_ID` — Vapi outbound phone number ID
- `TWILIO_ACCOUNT_SID` — Twilio account SID
- `TWILIO_AUTH_TOKEN` — Twilio auth token
- `TWILIO_PHONE_NUMBER` — Twilio outbound number

## Service Management

```bash
# Start everything (audio-assist + audio-forward + mic-forward)
scripts/audio-assist-service.sh start

# Stop everything
scripts/audio-assist-service.sh stop

# Check status
scripts/audio-assist-service.sh status

# Restart
scripts/audio-assist-service.sh restart
```

## Checking Call Logs

### Vapi Call Details (after a call)
```bash
# Get full call details including transcript, recording, and cost
source <(grep -E '^VAPI_API_KEY' ~/mark-sebast/apps/phone-agent/.env | sed 's/^/export /')
curl -sS -H "Authorization: Bearer $VAPI_API_KEY" \
    "https://api.vapi.ai/call/<CALL_ID>" | python3 -m json.tool

# List recent calls
curl -sS -H "Authorization: Bearer $VAPI_API_KEY" \
    "https://api.vapi.ai/call?limit=5" | python3 -m json.tool
```

Key fields in the Vapi response:
- `transcript` — full conversation text (AI: / User: turns)
- `summary` — AI-generated call summary
- `recordingUrl` — link to the call recording WAV file
- `costBreakdown` — breakdown by stt, llm, tts, vapi, transport
- `analysis.successEvaluation` — whether the call was successful
- `endedReason` — why the call ended (customer-ended-call, silence-timed-out, etc.)
- `messages` — timestamped message array with per-turn timing

### Local Transcripts (audio-assist service)
```bash
# Local transcripts are stored in data/audio_assist.db (SQLite)
# These capture from desk-mic/bose-mic and app-audio sources
# (separate from the Vapi call audio, which goes through the phone network)

# Query recent transcripts
python3 -c "
import sqlite3
db = sqlite3.connect('data/audio_assist.db')
for r in db.execute('''
    SELECT started_at, source_id, speaker, text
    FROM transcripts ORDER BY id DESC LIMIT 20
'''):
    print(f'[{r[0][:19]}] ({r[1]}) {r[2] or \"?\"}: {r[3]}')
db.close()
"

# Query transcript sessions (grouped conversations)
python3 -c "
import sqlite3
db = sqlite3.connect('data/audio_assist.db')
for r in db.execute('''
    SELECT session_id, source_id, started_at, ended_at, text
    FROM transcript_sessions ORDER BY id DESC LIMIT 5
'''):
    print(f'[{r[2][:19]} - {r[3][:19]}] ({r[1]}) {r[4][:100]}...')
db.close()
"
```

### mic-forward Stats
```bash
# Stats are logged every 10 seconds to data/logs/mic-forward.log
tail -20 data/logs/mic-forward.log

# Key stats fields:
#   ring=N frames (Xms)  — ring buffer fill = pipeline latency
#   in=N cb/M frames     — input callback count / total frames received
#   out=N cb/M frames    — output callback count / total frames sent
#   underruns=N          — times output had no data (should be 0 after startup)
```

## Test Call Log

### 2026-02-15 00:16 — C mic-forward validation call (SUCCESS)
- **Call ID**: `019c5fba-119a-7119-8548-cc4e9cdc0ffc`
- **Duration**: 72 seconds (05:16:02 - 05:17:14 UTC)
- **Ended**: customer-ended-call
- **Cost**: $0.11 (stt=$0.01, llm=$0.00, tts=$0.03, vapi=$0.06)
- **Mic**: Bose QC45 (16kHz 1ch -> 48kHz 2ch via C daemon)
- **Pipeline**: mic-forward (C) -> CaptureMic 2ch
- **Ring buffer latency during call**: 12.0 - 26.7ms (avg ~19ms)
- **Underruns during call**: 0 (3 total, all at startup)
- **CPU during call**: 0.0%
- **User feedback**: "fantastic from my side"
- **Vapi evaluation**: successEvaluation=true
- **Recording**: https://storage.vapi.ai/019c5fba-119a-7119-8548-cc4e9cdc0ffc-1771132637011-905ff161-f7bb-44fa-9241-5aff4555cd7a-mono.wav

**Transcript:**
```
AI: Hello. I'm an AI assistant joining the call. How can I help?
User: Hey. Can you hear me?
AI: Yes. I can hear you clearly. How's everything going so far?
User: Good. I'm gonna change devices. Let's just hold on 1 second.
AI: Go ahead and switch. I'll wait.
User: Okay. Can you hear me still?
AI: Yes, your voice is coming through fine on the new device.
User: Right. I can still hear you fine in the in the lightness sounds fine.
AI: Audio
User: What have you been up to today?
AI: Just assisting with various calls and tasks? And now I'm here helping with this call.
User: Oh, you how about you? You've been today?
AI: I'm just an AI, so I don't have personal experiences, but I've been helping users
    like you with their questions and tasks throughout the day.
User: And you're a great AI. Right?
AI: Thanks for the compliment. I'm here to help and provide useful information to the
    best of my abilities.
User: You know what? Thousand times 1000 is
AI: What's up? That's 1000001 0 0 0 0 0 0.
```

**mic-forward stats during call:**
```
00:15:49 ring=1024 (21.3ms), in=10 cb, out=19 cb, underruns=3
00:16:00 ring=1152 (24.0ms), in=528 cb, out=990 cb, underruns=3
00:16:10 ring= 832 (17.3ms), in=1045 cb, out=1960 cb, underruns=3
00:16:20 ring= 576 (12.0ms), in=1562 cb, out=2928 cb, underruns=3
00:16:31 ring=1152 (24.0ms), in=2080 cb, out=3900 cb, underruns=3
00:16:41 ring=1280 (26.7ms), in=2598 cb, out=4871 cb, underruns=3
00:16:51 ring=1024 (21.3ms), in=3114 cb, out=5839 cb, underruns=3
00:17:02 ring= 704 (14.7ms), in=3632 cb, out=6809 cb, underruns=3
```

### Previous calls (for reference)
| Call ID | Time (UTC) | Ended | Cost |
|---------|------------|-------|------|
| 019c5f8c... | 2026-02-15 04:26 | customer-ended | $0.19 |
| 019c5f7c... | 2026-02-15 04:08 | silence-timed-out | $0.09 |
| 019c5f78... | 2026-02-15 04:03 | customer-ended | $0.24 |
| 019c5f37... | 2026-02-15 02:53 | silence-timed-out | $0.05 |

## Performance: Python vs C mic-forward

| Metric | Python (old) | C (current) |
|--------|-------------|-------------|
| Pipeline latency | 60-90ms | **13-21ms** |
| Max write spike | 147ms (GIL/GC) | <2ms |
| CPU usage | ~5% | **0.0%** |
| Memory | ~80 MB | **14.5 MB** |
| Device detection | 2s poll + subprocess | Instant (native) |
| Resample quality | Linear interpolation | Polyphase sinc (Apple) |
| Underruns (steady) | Frequent | **0** |
| Audio glitches | Occasional | **0 detected** |
| Call quality | Noticeable delay | **"fantastic"** |

## Troubleshooting

### mic-forward won't start
```bash
# Check if CaptureMic 2ch driver is installed
ffmpeg -f avfoundation -list_devices true -i "" 2>&1 | grep CaptureMic

# Rebuild if needed
cd driver && make -f Makefile.mic-forward clean && make -f Makefile.mic-forward
```

### No audio from CaptureMic
```bash
# Check mic-forward is running
ps aux | grep mic-forward

# Check logs
tail -20 data/logs/mic-forward.log

# Verify ring buffer stats (should show non-zero fill)
# Stats logged every 10 seconds
```

### Call doesn't connect
```bash
# Check Vapi/Twilio credentials
cat ~/mark-sebast/apps/phone-agent/.env | grep -E '^(VAPI|TWILIO)' | cut -d= -f1
```

### Local transcripts not appearing
```bash
# audio-assist captures from desk-mic and app-audio, NOT from the phone call
# Vapi call audio goes through the phone network, not the local audio pipeline
# Check audio-assist is running:
scripts/audio-assist-service.sh status

# The Vapi transcript is available via the Vapi API (see "Checking Call Logs" above)
```
