# OpenClaw-Inspired Microservice Plan (Local + Secure + Personal)

## Why this doc

Use `openclaw` as architecture inspiration, not as a blueprint. The goal here is a smaller, personal system focused on reliable audio capture, transcript quality, and local-first operations.

## What I reviewed

Local clone reviewed at:
- `/Users/tdeshane/personal-assistant/openclaw`
- HEAD: `8933010e8`
- Size: ~`252M`
- Files: ~`4910`

High-signal references:
- `/Users/tdeshane/personal-assistant/openclaw/docs/concepts/architecture.md`
- `/Users/tdeshane/personal-assistant/openclaw/docs/gateway/security/index.md`
- `/Users/tdeshane/personal-assistant/openclaw/docs/concepts/session.md`
- `/Users/tdeshane/personal-assistant/openclaw/docs/concepts/memory.md`
- `/Users/tdeshane/personal-assistant/openclaw/src/gateway/*`

## Keep vs avoid

Borrow:
1. Single control-plane mindset.
2. Loopback-first networking and explicit auth for anything remote.
3. Session/source state as source-of-truth in one place.
4. Security audit posture (automated checks for risky config).
5. Optional capability services that never block core messaging/capture.

Do not copy (for now):
1. Multi-channel messaging surface explosion.
2. Large plugin ecosystem and in-process extension complexity.
3. Cross-device/node orchestration (mobile/mac nodes) until core audio is solid.
4. Broad product UX surface (webchat, social connectors, etc.).

## Target architecture for this repo

Start with a small set of focused services and strict fallbacks.

```mermaid
flowchart LR
  UI[Console + Transcript UI]
  GW[assistant-gateway\n(control plane)]
  CAP[audio-capture service]
  ASR[asr service\n(realtime + batch)]
  STORE[transcript-store service]
  QA[qa service\n(local model routing)]
  TTS[tts service]
  SPK[speaker service]

  UI --> GW
  GW --> CAP
  CAP --> ASR
  ASR --> STORE
  GW --> QA
  QA --> STORE
  QA --> TTS
  ASR --> SPK
  SPK --> STORE
```

## Service boundaries (v1)

### 1) `assistant-gateway` (new, thin)

Responsibility:
- single API/control plane for UI + future clients
- route requests to capture/asr/store/qa/tts/speaker
- expose status + health + queue lag in one place

Why:
- keeps orchestration centralized without forcing deep coupling
- mirrors the strongest OpenClaw idea without adopting full surface area

### 2) `audio-capture` (split from audio-assist over time)

Responsibility:
- enumerate devices, start/stop sources, level monitoring
- persistent WAV segment writing (must never drop silently)
- source/session lifecycle events

Hard rule:
- audio write path cannot depend on ASR/speaker/translation availability

### 3) `asr` (evolve from current transcriber + postprocess)

Responsibility:
- low-latency realtime transcription for live UI
- higher-accuracy post-process over archived WAV
- per-source decode profiles (mic vs system audio)
- language ID + raw transcript storage (original language only)

Hard rule:
- translation is enrichment, never overwrite raw text

### 4) `transcript-store` (logical service; can remain in-process initially)

Responsibility:
- canonical transcript/session/source records
- versioned transcript passes (`realtime`, `postprocess-v1`, ...)
- enrichment tables (`translation`, `speaker`, etc.)
- export APIs (raw-only, with translation, with speakers)

### 5) `qa` (current query path, tightened)

Responsibility:
- retrieval over transcript evidence
- grounded answer generation via local model provider(s)
- answer metadata (evidence ids, confidence, provider)

Hard rule:
- no answer should block capture/transcription pipeline

### 6) `tts` (already present)

Responsibility:
- speech synthesis from answer text
- optional streaming voice chunks
- warm-model behavior + fallback response if unavailable

### 7) `speaker` (already optional)

Responsibility:
- diarization/labeling as async enrichment
- backfill over archived WAV for better labels

Hard rule:
- never on critical path for capture or transcript persistence

## Security baseline (default local mode)

Adopt these as non-negotiable defaults:

1. Bind all services to `127.0.0.1` by default.
2. Token auth if any service is exposed beyond loopback.
3. Explicit allowlist for any remote ingress.
4. Local state dir permissions locked down (`700` dir / `600` sensitive files).
5. Add `assistant doctor` command to detect risky settings:
- non-loopback bind without auth
- open remote endpoints
- missing file permissions
- disabled transcript persistence
- capture queue drops above threshold

## Data policy (critical for trust)

1. Raw transcript is immutable and language-preserving.
2. Translation saved as separate enrichment fields.
3. Postprocess results are versioned, not destructive replacements.
4. Keep provenance: model id, pass type, timestamps, confidence summary.

## Phased rollout (slow + safe)

### Phase 0: stabilize current core
- Keep current `audio-assist` behavior as baseline.
- Add stronger runtime telemetry (capture lag, queue depth, dropped chunks).
- Ensure raw-first transcript display is default in UI.

### Phase 1: thin gateway layer
- Introduce `assistant-gateway` as API front-door.
- Keep existing services behind it with adapters.
- No behavioral changes; routing only.

### Phase 2: split capture and ASR concerns
- Extract capture lifecycle into `audio-capture` module/service.
- Keep ASR in dedicated path with source-specific profiles.
- Add replay/postprocess command over stored session audio.

### Phase 3: storage + enrichment hardening
- Formalize transcript versioning and enrichment tables.
- Speaker and translation remain async and optional.
- Add export modes and comparison views.

### Phase 4: only then consider more channels/integrations
- Add one integration at a time after core accuracy + reliability targets are met.

## Decision summary

The right path is **not** building an all-surface assistant yet.

The right path is:
- keep a small local control plane,
- harden capture/transcript reliability,
- separate raw transcription from enrichment,
- treat speaker/translation/voice as optional upgrades,
- expand outward only after quality metrics are stable.
