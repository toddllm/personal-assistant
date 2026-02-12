# google-sync-service

## Purpose

Local read-only Gmail + Google Calendar sync service.

Responsibilities:
- OAuth connect flow
- Gmail sync to local cache
- Calendar sync to local cache
- signal summary endpoints for assistant context

## Entrypoint

```bash
google-sync-service
```

Default bind:
- `127.0.0.1:8792`

## One-time setup

1. Create OAuth credentials in Google Cloud.
2. Add redirect URI:
- `http://127.0.0.1:8792/v1/auth/callback`
3. Configure either:
- `GOOGLE_SYNC_CLIENT_ID` and `GOOGLE_SYNC_CLIENT_SECRET`, or
- `GOOGLE_SYNC_CLIENT_SECRET_PATH`

## Connect flow

```bash
google-sync-connect
```

## Core endpoints

- `GET /health`
- `GET /connect`
- `GET /v1/auth/status`
- `POST /v1/auth/start`
- `GET /v1/auth/callback`
- `POST /v1/gmail/sync`
- `GET /v1/gmail/latest`
- `GET /v1/signals/summary`
- `POST /v1/calendar/sync`
- `GET /v1/calendar/latest`
- `GET /v1/calendar/events`

## Logging

Config prefix: `GOOGLE_SYNC_`

- `GOOGLE_SYNC_LOG_LEVEL`
- `GOOGLE_SYNC_LOG_JSON`
- `GOOGLE_SYNC_LOG_FILE_PATH`
- `GOOGLE_SYNC_LOG_FILE_MAX_BYTES`
- `GOOGLE_SYNC_LOG_FILE_BACKUP_COUNT`
- `GOOGLE_SYNC_ACCESS_LOG`

Default log path:
- `data/logs/google-sync-service.log`

## Security notes

- Loopback-only by default.
- OAuth scopes default to read-only Gmail + Calendar.
- Token/cache files are local-only and should not be committed.
