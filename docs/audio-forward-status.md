# Audio Forwarding Daemon — Implementation Status

## Current State
- [x] Config added to `src/audio_assist/config.py` (audio_forward_* settings)
- [x] Daemon created: `scripts/audio-forward.py`
- [x] Service script updated: `scripts/audio-assist-service.sh` (start/stop lifecycle)
- [x] ADR written: `docs/adr-0002-audio-capture-architecture.md`
- [x] Stop running services
- [x] Start services (audio-assist + audio-forward)
- [x] Verify CaptureAudio 2ch is system default output
- [x] Both capture sources confirmed running (desk-mic + app-audio)
- [x] MacBook Pro Speakers forwarding active (2ch @ 48kHz) — confirmed working
- [x] Bose QC45 forwarding active (stereo->mono down-mix @ 16kHz) — confirmed working
- [x] Volume control service works for both targets
- [x] app-audio capturing at -22.8 dBFS (strong signal)
- [x] desk-mic capturing at -60.6 dBFS (ambient, as expected)
- [x] Transcription active: 73 recent segments, 27 with text
- [x] End-to-end validation complete
- [x] Bose QC45 mic added as permanent autostart source (bose-mic)
- [x] All three sources auto-start on service restart confirmed
- [x] ADR updated to reflect three-source full-context architecture

## Issues Found
1. **Bose QC45 is mono output at 16kHz** — original code assumed 2ch/48kHz for all targets. Fixed: daemon now queries each target's actual capabilities and adapts channels + sample rate. Stereo->mono down-mix via channel averaging.

## Decisions Made During Testing
1. **Forward to ALL target devices simultaneously**, not just one. User controls what they hear via volume control service. Config: comma-separated `AUDIO_ASSIST_AUDIO_FORWARD_TARGET_DEVICES`.
2. **Collect from everything, always.** Three capture sources (desk-mic, app-audio, bose-mic) all autostart. Disconnected devices retry silently. No manual switching needed day-to-day. Full context is the only way the assistant is useful.
3. **Twilio test caller works.** `scripts/test-call.py` can call into Zoom with meeting ID + passcode via DTMF. TTS phrase "quick brown fox..." was captured and transcribed by app-audio. Full end-to-end validated: Twilio → Zoom → CaptureAudio → app-audio → transcription.

4. **Vapi interactive AI call works.** `scripts/ai-call.py --phone` calls directly via Vapi outbound API with inline assistant config (groq/llama-3.3-70b + 11labs voice). User had a real conversation with the AI. Note: Python urllib sends headers Vapi rejects (403), fixed by using curl subprocess.
5. **Vapi can't call Zoom dial-in numbers** (error 1010 / toll-free restriction). For Zoom, use `scripts/test-call.py` (Twilio TTS) instead. Interactive AI in Zoom would need a conference bridge or Twilio Media Streams — future work.

## TODO
- [ ] Source attribution: transcripts from Zoom calls should be identifiable as "from a Zoom call" rather than generic app-audio. Could use Zoom API, calendar matching, or active window detection to tag segments with the originating app.
- [ ] Interactive AI in Zoom: bridge Twilio (DTMF) + Vapi (AI) via Twilio Conference or Media Streams
