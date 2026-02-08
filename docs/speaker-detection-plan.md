# Speaker Detection Plan (Audio Assist)

## Current Integration

`audio-assist` now supports optional speaker labeling via a separate microservice:

- Config:
  - `AUDIO_ASSIST_SPEAKER_ENABLED=true`
  - `AUDIO_ASSIST_SPEAKER_SERVICE_URL=http://<host>:<port>`
- Status endpoint:
  - `GET /v1/speaker/status`
- If the speaker service is down, capture/transcription continues with no speaker labels.

## Expected Speaker Service Contract

`audio-assist` calls:

- `POST /v1/diarize/chunk`

Request JSON:

```json
{
  "source_id": "system-audio",
  "sample_rate": 16000,
  "started_at": "2026-02-08T20:00:00Z",
  "ended_at": "2026-02-08T20:00:04.5Z",
  "pcm_s16le_base64": "<base64 pcm bytes>"
}
```

Response JSON (either shape is accepted):

```json
{
  "speaker": "SPK_01",
  "confidence": 0.88
}
```

or

```json
{
  "segments": [
    {"speaker": "SPK_01", "start": 0.0, "end": 2.1, "confidence": 0.86},
    {"speaker": "SPK_02", "start": 2.1, "end": 4.5, "confidence": 0.80}
  ]
}
```

## First-Principles Priorities

1. Preserve capture reliability:
   - Speaker labeling must never block transcription.
   - Use short timeout and cooldown on repeated failures.
2. Keep stable speaker IDs inside a session:
   - Same person should map to same label (`SPK_01`, `SPK_02`, etc.) per source/session.
3. Optimize for meetings:
   - Low latency chunk-level diarization for “who said what”.
4. Handle noisy overlap:
   - Keep “unknown” label when confidence is low, rather than guessing.

## Model/Approach Options

1. Pyannote diarization pipeline (best quality baseline, GPU preferred).
2. WhisperX diarization alignment pipeline (good if already using whisper stack).
3. Lightweight VAD + speaker embedding clustering (faster, lower quality).

## Voice Cloning + Detection

Voice cloning can improve response voice UX, but speaker detection quality should be based on
speaker embeddings/diarization, not cloned voices. If cloning is added, keep it as a separate
microservice and only use it for output TTS personalization.
