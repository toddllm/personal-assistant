# audio-assist service

## Purpose

Core capture service for the personal assistant.

Responsibilities:
- ingest mic/system audio sources
- produce incremental transcript chunks
- store transcripts and archived audio locally
- expose transcript, query, and events UIs
- expose capture readiness diagnostics

## Entrypoint

```bash
audio-assist
```

Default bind:
- `127.0.0.1:8787`

## Managed local service

```bash
scripts/audio-assist-service.sh start
scripts/audio-assist-service.sh status
scripts/audio-assist-service.sh readiness
```

## Health and readiness endpoints

- `GET /health`
- `GET /v1/transcriber/status`
- `GET /v1/capture/readiness`

## Primary UI routes

- `GET /`
- `GET /transcripts`
- `GET /events`

## Logging

Config prefix: `AUDIO_ASSIST_`

- `AUDIO_ASSIST_LOG_LEVEL`
- `AUDIO_ASSIST_LOG_JSON`
- `AUDIO_ASSIST_LOG_FILE_PATH`
- `AUDIO_ASSIST_LOG_FILE_MAX_BYTES`
- `AUDIO_ASSIST_LOG_FILE_BACKUP_COUNT`
- `AUDIO_ASSIST_ACCESS_LOG`

Default logs:
- app: `data/logs/audio-assist.log`
- supervisor: `data/logs/audio-assist-supervisor.log`

## Integration dependencies

Optional downstream dependencies:
- `tts-service` for spoken responses
- `speaker-service` for speaker labels
- `google-sync-service` for calendar/email context

Capture and transcript persistence must remain functional if optional dependencies are unavailable.
