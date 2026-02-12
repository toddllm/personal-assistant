# tts-service

## Purpose

Speech synthesis microservice for assistant voice output.

## Entrypoint

```bash
tts-service
```

Default bind:
- `0.0.0.0:8790`

## Health endpoints

- `GET /health`
- `GET /v1/voices`

Synthesis endpoints:
- `POST /v1/synthesize`
- `POST /v1/synthesize/stream`
- `GET /v1/audio/{file_name}`

## Providers

- `qwen3_custom_voice` (default)
- `stub` (fallback)

## Logging

Config prefix: `TTS_SERVICE_`

- `TTS_SERVICE_LOG_LEVEL`
- `TTS_SERVICE_LOG_JSON`
- `TTS_SERVICE_LOG_FILE_PATH`
- `TTS_SERVICE_LOG_FILE_MAX_BYTES`
- `TTS_SERVICE_LOG_FILE_BACKUP_COUNT`
- `TTS_SERVICE_ACCESS_LOG`

Default log path:
- `data/logs/tts-service.log`

## Runtime notes

- Startup model preload is enabled by default (`TTS_SERVICE_STARTUP_LOAD_MODEL=true`).
- If model load fails, service starts in degraded mode and returns model load error via `/health`.
