# Native Zoom AI Integration Options

Our current approach uses a **PSTN phone bridge** (Twilio Conference + Vapi) to get an AI assistant into Zoom meetings. This works but adds telephony latency and cost. Below are all the options for native integration — getting audio/video streams directly from Zoom without the phone network.

## Comparison

| Approach | Bidirectional Audio | Cost | Complexity | Best For |
|----------|:---:|------|------------|----------|
| **Recall.ai** | Yes | ~$0.50/hr | Low (REST API) | Getting started fast |
| **Meeting BaaS** (Attendee.dev, Nylas) | Yes | ~$0.69/hr | Low | Alternative to Recall |
| **Zoom Meeting SDK** (py-zoom-meeting-sdk) | Yes | Infra only | Very High (C++) | Self-hosting at scale |
| **Zoom RTMS** (Real-Time Media Streams) | Receive only | Free (Zoom plan) | Moderate | Transcription / listen-only |
| **Zoom SIP/CRC** (Certified Room Connector) | Yes | ~$49/mo/room | High | Legacy conference rooms |

## Detailed Breakdown

### 1. Recall.ai (Recommended Starting Point)

**What**: Managed API service that joins meetings as a bot participant and provides real-time audio/video streams.

**How it works**:
- REST API call to create a "bot" that joins a Zoom (or Google Meet, Teams) meeting
- Bot joins as a named participant
- Provides real-time audio via WebSocket (PCM 16-bit, 16kHz)
- Can play audio back into the meeting (bidirectional)
- Handles all the Zoom SDK complexity

**Pros**:
- Hours to implement, not weeks
- Cross-platform (Zoom, Meet, Teams, Webex)
- No infrastructure to manage
- Real-time audio streaming (low latency)
- Active speaker detection built in

**Cons**:
- Per-minute cost adds up at scale (~$0.50/hr)
- Third-party dependency
- Bot appears as a separate participant (user sees "AI Bot" in participant list)

**Integration**:
```python
# Create a bot that joins a Zoom meeting
import requests

bot = requests.post("https://api.recall.ai/api/v1/bot/", json={
    "meeting_url": "https://zoom.us/j/89105007950?pwd=...",
    "bot_name": "AI Assistant",
    "real_time_transcription": {"partial_results": True},
    "transcription_options": {"provider": "meeting_captions"},
}, headers={"Authorization": "Token YOUR_API_KEY"}).json()

# Stream audio via WebSocket
# ws://api.recall.ai/api/v1/bot/{bot_id}/audio
```

### 2. Meeting BaaS (Attendee.dev, Nylas, Baas.dev)

**What**: Similar to Recall.ai — managed bots that join meetings.

**How it works**: Same concept as Recall. Attendee.dev and Baas.dev are newer entrants. Nylas acquired and integrated meeting bot technology.

**Pros**:
- Comparable features to Recall.ai
- Some offer differentiated pricing or features

**Cons**:
- Smaller ecosystem than Recall
- Same bot-as-participant UX

### 3. Zoom Meeting SDK (py-zoom-meeting-sdk)

**What**: Self-hosted bot using Zoom's native C++ Meeting SDK, wrapped in Python.

**How it works**:
- Uses the [zoom-meeting-sdk-linux](https://github.com/nicholasgasior/py-zoom-meeting-sdk) C++ library
- Bot runs as a headless Linux process
- Joins meeting natively via SDK (not as phone participant)
- Raw audio frames in/out at 16kHz
- Requires Zoom Marketplace app (Meeting SDK type)

**Pros**:
- No per-minute API costs (just your infrastructure)
- Lowest latency — direct SDK access
- Full control over audio pipeline
- Can access meeting events, chat, participants

**Cons**:
- Very complex setup (C++ SDK + Python bindings + Linux only)
- Must maintain infrastructure (Docker, auto-scaling)
- Zoom SDK updates can break things
- Requires Zoom Marketplace app approval for production

**When to use**: You've validated the product with Recall.ai and need to reduce per-minute costs at scale (hundreds of hours/month).

### 4. Zoom RTMS (Real-Time Media Streams)

**What**: Zoom's official API for streaming meeting media in real-time. Released 2024.

**How it works**:
- Register a webhook for meeting events
- When a meeting starts, request media streams via Zoom API
- Receive audio (and optionally video) via WebSocket
- Audio is per-speaker, already separated

**Pros**:
- Official Zoom API (supported, documented)
- No bot participant visible in meeting
- Per-speaker audio streams (great for transcription)
- Free with Zoom Pro plan
- No infrastructure to host a bot

**Cons**:
- **Receive-only** — cannot send audio back into the meeting
- Not suitable for voice agents (can only listen, not speak)
- Requires Zoom Marketplace app with RTMS scope
- Relatively new, API may evolve

**When to use**: Transcription, meeting notes, or analytics — any use case where you only need to listen.

### 5. Zoom SIP/CRC (Certified Room Connector)

**What**: SIP-based integration for connecting conference room hardware to Zoom.

**How it works**:
- Zoom provides SIP URIs for meetings
- Your SIP endpoint (e.g., FreeSWITCH, Opalstack) dials in
- Standard SIP/RTP audio — bidirectional
- Essentially a VoIP version of the phone bridge

**Pros**:
- Bidirectional audio
- Standard SIP tooling
- Lower latency than PSTN

**Cons**:
- Requires Zoom CRC add-on (~$49/month/room)
- Complex SIP infrastructure
- Designed for room systems, not bots
- Zoom may restrict programmatic use

## Recommended Path

1. **Now**: Keep the Twilio Conference bridge (current `ai-call.py`) — it works, it's reliable
2. **Next**: Integrate **Recall.ai** for native Zoom audio — takes hours, not weeks, and works across Zoom/Meet/Teams
3. **Later**: If costs grow (>$500/mo on Recall), self-host with **py-zoom-meeting-sdk** on a Linux server

## References

- [Recall.ai Docs](https://docs.recall.ai/)
- [Zoom RTMS Overview](https://developers.zoom.us/docs/meeting-sdk/apis/rtms/)
- [py-zoom-meeting-sdk](https://github.com/nicholasgasior/py-zoom-meeting-sdk)
- [Zoom Meeting SDK](https://developers.zoom.us/docs/meeting-sdk/)
- [Zoom SIP/CRC](https://support.zoom.us/hc/en-us/articles/201363location-for-CRC)
