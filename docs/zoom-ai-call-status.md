# Zoom AI Call — Implementation Status

**Date:** 2026-02-15

## Architecture: Twilio Conference Bridge

```
Zoom Meeting
    ↕  (PSTN + DTMF)
[Twilio Leg 1]  ──→  Conference "ai-zoom-{meeting_id}"  ←──  [Twilio Leg 2]
                       (echo cancellation + mixing)              ↕
                                                          Vapi AI Assistant
                                                        (ElevenLabs + Groq)
```

- **Leg 1**: Twilio calls Zoom dial-in, enters meeting via DTMF, joins a Conference
- **Leg 2**: Twilio calls Vapi inbound (+15183189215), joins same Conference
- Conference handles echo cancellation between the two legs
- `endConferenceOnExit="true"` tears down both legs when either hangs up

## Phone Numbers

| Number | Owner | Purpose |
|--------|-------|---------|
| **+15186224029** | Twilio (TEST-Voicemail) | Caller ID for Zoom calls (`--from-number`) |
| **+15187329522** | Twilio (TEST-LiveAnswer) | Alternative Twilio number |
| **+18885345217** | Twilio / Vapi outbound | Default TWILIO_PHONE_NUMBER, also Vapi outbound |
| **+15183189215** | Vapi inbound | AI assistant answers here (Leg 2 target) |
| **+15183868048** | Personal phone | Todd's phone for direct call testing |
| **+16465588656** | Zoom | Zoom US (New York) dial-in number |

## Vapi Assistant Configuration

| ID | Name | Purpose |
|----|------|---------|
| `f4cac0a6-4dfc-4d18-b82b-162cb030c544` | AI Call Assistant | **Active** — ElevenLabs voice, Groq LLM, meeting assistant |
| `293b4c6a-b1ea-4940-a388-b948094da2bf` | Don Brown Promo | Old promo assistant — was incorrectly being used for Zoom calls |

The inbound number (+15183189215) has `assistantId` pointing to the AI Call Assistant.
The `update_vapi_phone_assistant()` function PATCHes the saved assistant directly
(Vapi ignores inline assistant overrides when `assistantId` is set on the phone number).

## Bugs Fixed (2026-02-15)

### 1. Wrong Vapi phone number being PATCHed
- **Symptom**: AI joined Zoom with "Don Brown Promo" script instead of meeting assistant
- **Root cause**: `VAPI_PHONE_NUMBER_ID` in .env pointed to the 888 outbound number, not the 518 inbound number
- **Fix**: Added `VAPI_INBOUND_PHONE_ID` constant with the correct ID (`79b4be41-be3c-45b3-bfad-aca166214570`)

### 2. Vapi ignoring inline assistant config
- **Symptom**: PATCH with `assistantId: null` + `assistant: {...}` didn't work — assistantId persisted
- **Root cause**: Vapi API doesn't clear `assistantId` when set to null via PATCH
- **Fix**: Created a new saved assistant (`f4cac0a6-...`) with correct config, pointed inbound number to it. Changed `update_vapi_phone_assistant()` to PATCH the saved assistant directly.

### 3. Echo in Zoom calls
- **Symptom**: Echo heard during AI conversation in Zoom
- **Root cause**: Zoom was using the wrong audio device (mic and speaker)
- **Fix**: Updated Zoom audio device settings — selected correct mic and speaker. Conference bridge echo cancellation works correctly once the right audio devices are set.

### 4. Silence timeout on early attempts
- **Symptom**: Vapi hung up after ~30s with `silence-timed-out`
- **Root cause**: 5-second wait between legs was too short — Zoom IVR/DTMF takes ~12-15s, so Vapi joined before Zoom audio was flowing
- **Fix**: Increased wait to 15s between legs, increased silence timeout to 60s

### 5. Caller ID showing wrong number
- **Symptom**: Zoom showed the 888 toll-free number as caller
- **Fix**: Added `--from-number` CLI arg to override caller ID, use +15186224029 (518 number)

## Latency Analysis

Direct phone call has minimal latency (single PSTN hop):
```
User → Phone → Vapi (STT→LLM→TTS) → Phone → User
```

Zoom Conference bridge adds ~200-400ms round-trip due to extra hops:
```
User → Zoom → PSTN → Twilio Leg 1 → Conference → Twilio Leg 2 → PSTN → Vapi → (processing) → reverse path
```

This latency is inherent in the phone bridge architecture. For lower latency,
see `docs/zoom-native-integration.md` for native Zoom integration options
(Recall.ai, Meeting SDK, RTMS).

## Test Results

### Direct phone call (working perfectly)
```
scripts/ai-call.py --phone "+15183868048"
```
- ElevenLabs voice, correct prompt
- No echo, minimal latency
- Vapi Call ID: 019c600d-52a6-7559-9aa7-77057cbc0cb2

### Zoom call (working with known latency)
```
scripts/ai-call.py --zoom-number "+16465588656" --meeting-id "89105007950" \
  --passcode "857908" --from-number "+15186224029"
```
- ElevenLabs voice, correct prompt
- No echo (after Zoom audio device fix)
- Noticeable latency vs direct call (~200-400ms extra round-trip)
- Zoom leg SID: CA720fc100cc4adfa96acbe898a34635e1
- Vapi leg SID: CA24684109e9941030f9f5a99875c53a09

### Zoom call cost breakdown (Vapi)
- Total: $0.22 for ~2m46s
- LLM: $0.003, STT: $0.028, TTS: $0.053, Vapi: $0.139

## CLI Reference

```bash
# AI joins Zoom meeting
scripts/ai-call.py --zoom-number "+16465588656" --meeting-id "89105007950" \
  --passcode "857908" --from-number "+15186224029"

# AI calls phone directly
scripts/ai-call.py --phone "+15183868048"

# Check call status
scripts/ai-call.py --status <CALL_SID>

# TTS test call (no AI, just speaks a message)
scripts/test-call.py --phone "+15183868048" --message "Testing"

# TTS into Zoom
scripts/test-call.py --zoom-number "+16465588656" --meeting-id "89105007950" \
  --passcode "857908" --message "Testing"
```
