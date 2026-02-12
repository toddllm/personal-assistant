# speaker-service

## Purpose

Optional speaker diarization/enrichment service used by `audio-assist`.

## Entrypoint

```bash
speaker-service
```

Default bind:
- `0.0.0.0:8791`

## Health endpoint

- `GET /health`

Core API:
- `POST /v1/diarize/chunk`

## Logging

Config prefix: `SPEAKER_SERVICE_`

- `SPEAKER_SERVICE_LOG_LEVEL`
- `SPEAKER_SERVICE_LOG_JSON`
- `SPEAKER_SERVICE_LOG_FILE_PATH`
- `SPEAKER_SERVICE_LOG_FILE_MAX_BYTES`
- `SPEAKER_SERVICE_LOG_FILE_BACKUP_COUNT`
- `SPEAKER_SERVICE_ACCESS_LOG`

Default log path:
- `data/logs/speaker-service.log`

## Runtime notes

- This service is intentionally optional.
- `audio-assist` should continue transcription even if this service is down or slow.
