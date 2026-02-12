# Repo Health

This repo uses a lightweight health gate before commits and releases.

## Standard Check

Run:

```bash
scripts/repo-health.sh
```

This executes:
- Python bytecode compile check (`compileall`) for `src/` and `tests/`
- `ruff` lint checks
- `pytest` test suite

## Commit Gate

Before committing production-impacting changes:

1. Run `scripts/repo-health.sh`
2. Confirm `GET /health` returns `{"status":"ok"}`
3. Confirm `GET /v1/capture/readiness` reports `status: ready` during an active audio test
4. Confirm service logs are writable and rotating under `data/logs/`
5. Save any investigation outputs in `data/exports/` if needed for debugging traceability

## Operational Gate (Capture Critical)

Before important meetings/sessions:

1. `scripts/audio-assist-service.sh status`
2. `scripts/audio-assist-service.sh readiness`
3. Speak a short test phrase and verify new transcript rows in `data/audio_assist.db`
4. Confirm recent log lines in `data/logs/audio-assist.log`

## Log Paths

- `audio-assist`: `data/logs/audio-assist.log`
- `google-sync-service`: `data/logs/google-sync-service.log`
- `email-service`: `data/logs/email-service.log`
- `tts-service`: `data/logs/tts-service.log`
- `speaker-service`: `data/logs/speaker-service.log`
