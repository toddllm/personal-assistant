# Voice Detection Research (Post-TTS Integration)

## Summary

`Qwen3-TTS` is strong for generation, voice design, and voice cloning, but it is not a full speaker-identification/verification stack by itself.

From the `toddllm` Qwen3-TTS code:
- `generate_voice_clone(...)` supports cloning from reference audio.
- `create_voice_clone_prompt(...)` supports `x_vector_only_mode=True` and internally extracts speaker embeddings.
- This means we can reuse clone/reference audio artifacts for speaker-matching workflows, but we still need a dedicated detection pipeline.

## Recommended Detection Microservice

### Service role

Create a separate microservice (for example `voice-detect-service`) that performs:
- speaker diarization (who spoke when)
- speaker embedding extraction
- enrollment and matching against known voices

### Suggested stack

- Diarization: `pyannote.audio` (segmenting speakers over time)
- Embeddings: `ECAPA-TDNN` (SpeechBrain) or `wespeaker`
- Matching: cosine similarity + configurable threshold
- Optional: keep reference clips produced during Qwen3-TTS cloning as enrollment samples

### Proposed API sketch

- `POST /v1/enroll`
  - input: `speaker_id`, `audio` (wav/base64), metadata
  - output: enrolled profile id
- `POST /v1/detect`
  - input: audio chunk/stream id
  - output: timeline of speaker segments + match scores
- `GET /v1/profiles`
  - output: enrolled speaker list

### Current baseline in this repo

This repo now includes an optional `speaker-service` with a lightweight clustering baseline:
- endpoint-compatible with `POST /v1/diarize/chunk`
- returns stable per-session labels such as `SPK_01`, `SPK_02`
- intended for local experimentation and low-friction fallback, not final production accuracy
- `audio-assist` can also run archive-assisted backfill to relabel recent unlabeled transcript rows as more WAV chunks accumulate

## How Qwen3-TTS helps detection

Qwen3-TTS cloned voice workflow can provide:
- curated reference clips
- metadata (`ref_text`, language, clone mode)
- speaker embedding extraction path (x-vector flow) in existing code

Use this as an enrollment source, not as the only detector.

## Practical rollout

1. Keep TTS service separate and stable first.
2. Build voice detection service with diarization + embedding matching.
3. Connect `audio-assist` to attach `speaker_id` tags to transcript chunks.
4. Add threshold tuning and false-positive evaluation per known speaker.
