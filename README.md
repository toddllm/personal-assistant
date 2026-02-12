# personal-assistant monorepo

Local-first personal assistant services for always-on capture, transcript review, event context, and optional voice + sync integrations.

## Monorepo decision

First-party services in this repo should stay in one monorepo (not git submodules).

- decision record: `docs/adr-0001-monorepo-service-structure.md`
- service boundaries: `docs/service-map.md`

Use git submodules only for third-party or independently owned codebases.

## Services

| Group | Service | Package | Entrypoint | Default Port | Service Doc |
|---|---|---|---|---:|---|
| Core capture | `audio-assist` | `src/audio_assist` | `audio-assist` | 8787 | `docs/services/audio-assist.md` |
| Context ingestion | `google-sync-service` | `src/google_sync_service` | `google-sync-service` | 8792 | `docs/services/google-sync-service.md` |
| Context ingestion | `email-service` | `src/email_service` | `email-service` | 8793 | `docs/services/email-service.md` |
| Voice output | `tts-service` | `src/tts_service` | `tts-service` | 8790 | `docs/services/tts-service.md` |
| Enrichment | `speaker-service` | `src/speaker_service` | `speaker-service` | 8791 | `docs/services/speaker-service.md` |

## Install

```bash
cd /Users/tdeshane/personal-assistant
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

## Run (recommended)

Run `audio-assist` as a managed local service:

```bash
scripts/audio-assist-service.sh start
scripts/audio-assist-service.sh status
scripts/audio-assist-service.sh readiness
```

UI routes:

```bash
open http://127.0.0.1:8787/
open http://127.0.0.1:8787/transcripts
open http://127.0.0.1:8787/events
```

## Optional services

### google-sync-service

```bash
source .venv/bin/activate
google-sync-service
```

Connect once:

```bash
google-sync-connect
```

Important:
- Gmail reads require Gmail API enabled in your Google Cloud project.
- If Gmail API is disabled, `POST /v1/gmail/sync` will return `403 accessNotConfigured`.

### email-service

```bash
source .venv/bin/activate
email-service
```

Open UI:

```bash
open http://127.0.0.1:8793/
```

Initial inbox refresh:

```bash
curl -sS -X POST http://127.0.0.1:8793/v1/inbox/refresh \
  -H "Content-Type: application/json" \
  -d '{"force_sync":true,"max_results":50,"label_ids":["INBOX"]}'
```

Email Q&A via Ollama:

```bash
curl -sS -X POST http://127.0.0.1:8793/v1/assistant/query \
  -H "Content-Type: application/json" \
  -d '{"question":"What should I reply to first?","focus_only":true,"max_messages":30}'
```

### tts-service

On local fallback mode:

```bash
source .venv/bin/activate
export TTS_SERVICE_PROVIDER=stub
tts-service
```

### speaker-service

```bash
source .venv/bin/activate
speaker-service
```

## Capture-critical preflight

Before an important meeting/session:

1. `scripts/audio-assist-service.sh status`
2. `scripts/audio-assist-service.sh readiness`
3. speak `hello test test test` and verify transcript rows appear in `/transcripts`
4. check service log tail:

```bash
tail -n 40 data/logs/audio-assist.log
```

## Logging

All services support rotating file logs + access log toggle.

Default log files:
- `data/logs/audio-assist.log`
- `data/logs/google-sync-service.log`
- `data/logs/email-service.log`
- `data/logs/tts-service.log`
- `data/logs/speaker-service.log`

Environment prefixes:
- `AUDIO_ASSIST_*`
- `GOOGLE_SYNC_*`
- `EMAIL_SERVICE_*`
- `TTS_SERVICE_*`
- `SPEAKER_SERVICE_*`

Each service supports:
- `<PREFIX>LOG_LEVEL`
- `<PREFIX>LOG_JSON`
- `<PREFIX>LOG_FILE_PATH`
- `<PREFIX>LOG_FILE_MAX_BYTES`
- `<PREFIX>LOG_FILE_BACKUP_COUNT`
- `<PREFIX>ACCESS_LOG`

## Health and test gates

Run full repo checks:

```bash
scripts/repo-health.sh
```

CI workflow:
- `.github/workflows/repo-health.yml`

Operational guide:
- `docs/repo-health.md`

## Key API endpoints

`audio-assist`:
- `GET /health`
- `GET /v1/capture/readiness`
- `GET /v1/transcriber/status`
- `POST /v1/sources/start`
- `POST /v1/sources/stop/{source_id}`
- `GET /v1/transcripts/page`
- `GET /v1/events/page`

`google-sync-service`:
- `GET /health`
- `GET /v1/auth/status`
- `POST /v1/gmail/sync`
- `POST /v1/calendar/sync`
- `GET /v1/calendar/events`

`email-service`:
- `GET /health`
- `GET /v1/assistant/status`
- `POST /v1/assistant/query`
- `POST /v1/inbox/refresh`
- `GET /v1/inbox/overview`
- `GET /v1/inbox/focus`
- `GET /v1/inbox/threads`
- `GET /v1/inbox/messages`

`tts-service`:
- `GET /health`
- `GET /v1/voices`
- `POST /v1/synthesize`
- `POST /v1/synthesize/stream`

`speaker-service`:
- `GET /health`
- `POST /v1/diarize/chunk`

## Docs index

- Architecture decision: `docs/adr-0001-monorepo-service-structure.md`
- Service map: `docs/service-map.md`
- Service runbooks: `docs/services/README.md`
- Repo health: `docs/repo-health.md`
- Epic: `docs/epics/productionize-audio-assist-epic.md`
