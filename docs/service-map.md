# Service Map

Current first-party services in this monorepo.

## Core capture plane

### audio-assist
- Package: `src/audio_assist`
- Entrypoint: `audio-assist`
- Port: `8787`
- Purpose: audio capture, realtime transcription, transcript/event UIs, readiness checks.
- Service doc: `docs/services/audio-assist.md`

## Context ingestion plane

### google-sync-service
- Package: `src/google_sync_service`
- Entrypoint: `google-sync-service`
- Port: `8792`
- Purpose: read-only Gmail + Google Calendar sync/cache for assistant context.
- Service doc: `docs/services/google-sync-service.md`

### email-service
- Package: `src/email_service`
- Entrypoint: `email-service`
- Port: `8793`
- Purpose: local inbox prioritization/focus layer on top of `google-sync-service`.
- Service doc: `docs/services/email-service.md`

## Voice and enrichment plane

### tts-service
- Package: `src/tts_service`
- Entrypoint: `tts-service`
- Port: `8790`
- Purpose: optional speech synthesis.
- Service doc: `docs/services/tts-service.md`

### speaker-service
- Package: `src/speaker_service`
- Entrypoint: `speaker-service`
- Port: `8791`
- Purpose: optional async speaker diarization/enrichment.
- Service doc: `docs/services/speaker-service.md`

## Boundaries
- `audio-assist` is core; other services are optional integrations.
- Capture/transcript persistence must remain functional if optional services are unavailable.
- Shared conventions:
  - local-first defaults
  - explicit health endpoints
  - rotating logs
  - repo-health checks before merge/release

## Repo layout
- `src/` service packages
- `scripts/` operator workflows
- `docs/services/` per-service runbooks
- `docs/` architecture, ADRs, epics, and operational standards
- `.github/workflows/` CI gates
