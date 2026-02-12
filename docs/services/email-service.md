# email-service

## Purpose

Local inbox prioritization service that builds a focused email view from `google-sync-service` Gmail data.

Responsibilities:
- trigger Gmail sync via `google-sync-service`
- persist a local inbox snapshot (`data/email_service/inbox_latest.json`)
- compute priority/focus lists for quick triage
- expose local-only endpoints for messages, threads, and focus queue

## Entrypoint

```bash
email-service
```

Default bind:
- `127.0.0.1:8793`

## Core endpoints

- `GET /health`
- `POST /v1/inbox/refresh`
- `GET /v1/inbox/overview`
- `GET /v1/inbox/focus`
- `GET /v1/inbox/threads`
- `GET /v1/inbox/messages`

## Data flow

1. `email-service` calls local `google-sync-service` (`/v1/gmail/sync` or `/v1/gmail/latest`).
2. Messages are scored/clustered locally.
3. Snapshot is written to local storage and served by `email-service`.

## Logging

Config prefix: `EMAIL_SERVICE_`

- `EMAIL_SERVICE_LOG_LEVEL`
- `EMAIL_SERVICE_LOG_JSON`
- `EMAIL_SERVICE_LOG_FILE_PATH`
- `EMAIL_SERVICE_LOG_FILE_MAX_BYTES`
- `EMAIL_SERVICE_LOG_FILE_BACKUP_COUNT`
- `EMAIL_SERVICE_ACCESS_LOG`

Default log path:
- `data/logs/email-service.log`

## Notes

- This POC is read-only and local-first.
- It depends on Gmail read access from `google-sync-service`.
- If Gmail sync fails, `email-service` can fall back to the last local snapshot.
