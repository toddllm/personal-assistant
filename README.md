# personal-assistant (audio + tts microservices)

This repo contains:
- `audio-assist`: always-listening transcription + retrieval + Q&A service
- `tts-service`: speech synthesis service (Qwen3-TTS compatible, recommended to run on toddllm)

## 1) Install

```bash
cd /Users/tdeshane/personal-assistant
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

## 2) Run audio-assist

```bash
audio-assist
```

Open UI:

```bash
open http://127.0.0.1:8787/
open http://127.0.0.1:8787/transcripts
open http://127.0.0.1:8787/events
```

UI defaults:
- common source presets are pre-populated (`desk-mic` + detected virtual loopback devices such as BlackHole/Loopback/Soundflower)
- `Add Source` starts selected preset
- `Auto-start common sources` starts default presets on page load
- voice playback supports chunked streaming (`Stream Voice` toggle)
- `/transcripts` provides paginated full-history transcript browsing with copy/download tools
- transcripts are stored as raw ASR output only (translation is optional and never overwrites raw text unless post-process apply is explicitly called)
- transcript APIs now default to session view (one growing transcript per capture session)
- default mic speaker label is `Todd Deshane` for source `desk-mic`
- transcript page defaults to interleaved timeline mode with timestamps; enable `Session view` toggle when needed
- transcript page includes an in-app `Google Calendar Matching` panel (connect, sync, refresh status)
- `/events` shows synced calendar events with overlapping transcript chunks attached per event (events still show when overlap is zero)
- speaker labeling is optional and best-effort; transcription continues if speaker service is slow/unavailable
- speaker backfill can relabel recent unlabeled chunks from archived WAV files as audio keeps growing

`audio-assist` defaults:
- host/port: `127.0.0.1:8787`
- transcript DB: `data/audio_assist.db`
- archived audio segments: `data/audio_segments/`
- whisper model: `base` (multilingual, lazy-loaded on first audio segment)
- post-process whisper model: `large-v3` (higher accuracy, slower; runs on archived WAV chunks)
- live chunk overlap: `0.6s` (improves boundary recall for fast speech/music)
- system audio VAD: disabled by default (higher recall for music-heavy streams)
- TTS service URL: `http://toddllm:8790`
- speaker service URL: `http://127.0.0.1:8791` (disabled by default)

### 2b) Run as a managed local service (recommended)

```bash
scripts/audio-assist-service.sh start
scripts/audio-assist-service.sh status
scripts/audio-assist-service.sh readiness
```

Logs:
- structured app logs: `data/logs/audio-assist.log`
- supervisor/stdout logs: `data/logs/audio-assist-supervisor.log`

Stop/restart:

```bash
scripts/audio-assist-service.sh stop
scripts/audio-assist-service.sh restart
```

Logging config env vars:
- `AUDIO_ASSIST_LOG_LEVEL` (default: `INFO`)
- `AUDIO_ASSIST_LOG_JSON` (default: `false`)
- `AUDIO_ASSIST_LOG_FILE_PATH` (default: `data/logs/audio-assist.log`)
- `AUDIO_ASSIST_LOG_FILE_MAX_BYTES` (default: `10485760`)
- `AUDIO_ASSIST_LOG_FILE_BACKUP_COUNT` (default: `7`)
- `AUDIO_ASSIST_ACCESS_LOG` (default: `true`)

## 3) Run tts-service

### Recommended: run on toddllm (Qwen3 model on GPU)

On toddllm:

```bash
cd /home/tdeshane/personal-assistant
# Uses the existing Qwen3-TTS environment that already has torch + qwen_tts
source /home/tdeshane/Qwen3-TTS/venv/bin/activate
pip install -e .
export TTS_SERVICE_PROVIDER=qwen3_custom_voice
export TTS_SERVICE_QWEN_MODEL_ID=Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice
export TTS_SERVICE_QWEN_IMPORT_PATH=/home/tdeshane/Qwen3-TTS
export TTS_SERVICE_QWEN_DEVICE_MAP=cuda:0
export TTS_SERVICE_QWEN_ATTN_IMPLEMENTATION=none
export TTS_SERVICE_PORT=8790
tts-service
```

On your local machine (where `audio-assist` runs):

```bash
# default is already http://toddllm:8790, override only if needed
export AUDIO_ASSIST_TTS_SERVICE_URL=http://toddllm:8790
audio-assist
```

If TTS is down/unreachable, `audio-assist` still returns text answers and includes `tts_error`.
`tts-service` now preloads the Qwen model at startup by default (`TTS_SERVICE_STARTUP_LOAD_MODEL=true`).

### Local fallback mode (no Qwen model required)

```bash
export TTS_SERVICE_PROVIDER=stub
tts-service
```

## 3b) Run speaker-service (optional)

Use this if you want chunk-level speaker labels (`SPK_01`, `SPK_02`, ...). It is lightweight and fully optional.

```bash
cd /Users/tdeshane/personal-assistant
source .venv/bin/activate
speaker-service
```

Then enable speaker detection in `audio-assist`:

```bash
export AUDIO_ASSIST_SPEAKER_ENABLED=true
export AUDIO_ASSIST_SPEAKER_SERVICE_URL=http://127.0.0.1:8791
# keep async mode so speaker detection never blocks transcription
export AUDIO_ASSIST_SPEAKER_ASYNC_ENRICHMENT=true
audio-assist
```

## 3c) Run google-sync-service (optional, Gmail + Calendar read-only)

This service ingests Gmail + Calendar signals into local cache and API endpoints.

Set either:
- `GOOGLE_SYNC_CLIENT_ID` + `GOOGLE_SYNC_CLIENT_SECRET`, or
- `GOOGLE_SYNC_CLIENT_SECRET_PATH` pointing to your Google OAuth client JSON.

The OAuth redirect URI must match:
- `http://127.0.0.1:8792/v1/auth/callback`

Run service:

```bash
cd /Users/tdeshane/personal-assistant
source .venv/bin/activate
google-sync-service
```

Or let `audio-assist` auto-start it as part of the app flow:

```bash
export AUDIO_ASSIST_CALENDAR_MATCH_ENABLED=true
export AUDIO_ASSIST_GOOGLE_SYNC_AUTOSTART=true
export AUDIO_ASSIST_GOOGLE_SYNC_CLIENT_SECRET_PATH="/Users/tdeshane/personal-assistant/google/client_secret_123.apps.googleusercontent.com.json"
audio-assist
```

One-step connect flow (opens browser; approve once):

```bash
google-sync-connect
```

After connect, run a sync:

```bash
curl -sS -X POST http://127.0.0.1:8792/v1/gmail/sync \
  -H "Content-Type: application/json" \
  -d '{"max_results":25,"label_ids":["INBOX"]}'

curl -sS -X POST http://127.0.0.1:8792/v1/calendar/sync \
  -H "Content-Type: application/json" \
  -d '{"calendar_id":"primary","max_results":250}'
```

## 4) Start listening

Mic source:

```bash
curl -sS -X POST http://127.0.0.1:8787/v1/sources/start \
  -H "Content-Type: application/json" \
  -d '{"source_type":"mic","source_id":"desk-mic","language_hint":"en"}'
```

Meeting/podcast via ffmpeg:

```bash
curl -sS -X POST http://127.0.0.1:8787/v1/sources/start \
  -H "Content-Type: application/json" \
  -d '{
    "source_type":"ffmpeg",
    "source_id":"system-audio",
    "language_hint":"es",
    "ffmpeg_input":":0",
    "ffmpeg_input_format":"avfoundation"
  }'
```

Update ASR language hint while running:

```bash
curl -sS -X POST http://127.0.0.1:8787/v1/sources/language \
  -H "Content-Type: application/json" \
  -d '{"source_id":"system-audio","language_hint":"es"}'
```

Live translation (keeps raw transcript unchanged):

```bash
curl -sS -X POST http://127.0.0.1:8787/v1/transcripts/translate \
  -H "Content-Type: application/json" \
  -d '{"transcript_ids":[3112,3108],"source_language":"es","target_language":"English"}'
```

List ffmpeg devices on macOS:

```bash
ffmpeg -f avfoundation -list_devices true -i ""
```

## 5) Query (text and optional voice)

Text answer:

```bash
curl -sS -X POST http://127.0.0.1:8787/v1/query \
  -H "Content-Type: application/json" \
  -d '{
    "question":"What action items were mentioned?",
    "translate":false,
    "provider":"ollama",
    "ollama_model":"llama3.1:8b",
    "since_seconds":1800
  }'
```

Text + spoken answer:

```bash
curl -sS -X POST http://127.0.0.1:8787/v1/query \
  -H "Content-Type: application/json" \
  -d '{
    "question":"Summarize this discussion.",
    "translate":false,
    "provider":"ollama",
    "ollama_model":"llama3.1:8b",
    "speak":true,
    "tts_voice":"Vivian",
    "tts_language":"English"
  }'
```

## 6) Key endpoints

audio-assist:
- `GET /health`
- `GET /` (frontend)
- `GET /transcripts` (full transcript manager page)
- `GET /events` (calendar events + transcript overlap page)
- `GET /v1/devices/mic`
- `GET /v1/ollama/models`
- `GET /v1/tts/status`
- `GET /v1/tts/voices`
- `GET /v1/speaker/status`
- `GET /v1/transcriber/status`
- `GET /v1/capture/readiness`
- `POST /v1/speaker/backfill`
- `POST /v1/tts/synthesize/stream` (SSE chunked voice stream)
- `GET /v1/sources`
- `POST /v1/sources/ensure`
- `POST /v1/sources/start`
- `POST /v1/sources/language` (set or clear per-source ASR language hint)
- `POST /v1/sources/stop/{source_id}`
- `GET /v1/transcripts/recent`
- `GET /v1/transcripts/page`
- `GET /v1/events/page`

## 7) Repo Health

Run all local quality gates:

```bash
scripts/repo-health.sh
```

Reference docs:
- repo health guide: `docs/repo-health.md`
- productionization epic: `docs/epics/productionize-audio-assist-epic.md`
- `GET /v1/transcripts/sources`
- `POST /v1/transcripts/postprocess` (re-transcribe archived WAV chunks; optional apply)
- `POST /v1/transcripts/translate` (translate selected transcript IDs; raw transcript is preserved)
- `POST /v1/query`
- `POST /v1/ingest/transcript`
- `POST /v1/ingest/pcm`

tts-service:
- `GET /health`
- `GET /v1/voices`
- `POST /v1/synthesize`
- `POST /v1/synthesize/stream` (SSE chunked synthesis)
- `GET /v1/audio/{file_name}`

speaker-service:
- `GET /health`
- `POST /v1/diarize/chunk`

google-sync-service:
- `GET /health`
- `GET /connect` (browser redirect for OAuth)
- `GET /v1/auth/status`
- `POST /v1/auth/start`
- `GET /v1/auth/callback`
- `POST /v1/gmail/sync`
- `GET /v1/gmail/latest`
- `GET /v1/signals/summary`
- `POST /v1/calendar/sync`
- `GET /v1/calendar/latest`
- `GET /v1/calendar/events`

## 7) Config

audio-assist env vars:
- `AUDIO_ASSIST_HOST`
- `AUDIO_ASSIST_PORT`
- `AUDIO_ASSIST_SAMPLE_RATE`
- `AUDIO_ASSIST_SEGMENT_SECONDS`
- `AUDIO_ASSIST_SEGMENT_OVERLAP_SECONDS` (default `0.6`)
- `AUDIO_ASSIST_WHISPER_MODEL`
- `AUDIO_ASSIST_WHISPER_DEVICE`
- `AUDIO_ASSIST_WHISPER_COMPUTE_TYPE`
- `AUDIO_ASSIST_WHISPER_LANGUAGE` (default `auto`, preserves original spoken language)
- `AUDIO_ASSIST_WHISPER_BEAM_SIZE` (default `1`)
- `AUDIO_ASSIST_WHISPER_BEST_OF` (default `1`)
- `AUDIO_ASSIST_WHISPER_VAD_FILTER` (default `true`)
- `AUDIO_ASSIST_WHISPER_SYSTEM_AUDIO_VAD_FILTER` (default `false`, higher recall for music/singing)
- `AUDIO_ASSIST_WHISPER_NO_SPEECH_THRESHOLD` (default `0.6`)
- `AUDIO_ASSIST_WHISPER_SYSTEM_AUDIO_NO_SPEECH_THRESHOLD` (default `1.0`, keeps low-confidence system audio from being dropped)
- `AUDIO_ASSIST_WHISPER_MIN_SIGNAL_DBFS` (default `-58.0`)
- `AUDIO_ASSIST_WHISPER_FALLBACK_ON_EMPTY` (default `true`)
- `AUDIO_ASSIST_WHISPER_FALLBACK_BEAM_SIZE` (default `2`)
- `AUDIO_ASSIST_WHISPER_FALLBACK_BEST_OF` (default `2`)
- `AUDIO_ASSIST_WHISPER_FALLBACK_VAD_FILTER` (default `false`)
- `AUDIO_ASSIST_WHISPER_FALLBACK_NO_SPEECH_THRESHOLD` (default `1.0`)
- `AUDIO_ASSIST_WHISPER_FALLBACK_MIN_SIGNAL_DBFS` (default `-55.0`)
- `AUDIO_ASSIST_TRANSCRIPTION_QUEUE_SIZE` (default `1024`)
- `AUDIO_ASSIST_TRANSCRIPTION_DROP_ARCHIVE_FALLBACK` (default `true`)
- `AUDIO_ASSIST_WHISPER_POSTPROCESS_MODEL` (default `large-v3`)
- `AUDIO_ASSIST_WHISPER_POSTPROCESS_DEVICE`
- `AUDIO_ASSIST_WHISPER_POSTPROCESS_COMPUTE_TYPE`
- `AUDIO_ASSIST_WHISPER_POSTPROCESS_LANGUAGE` (default `auto`)
- `AUDIO_ASSIST_WHISPER_POSTPROCESS_CPU_THREADS` (default `0`, auto)
- `AUDIO_ASSIST_WHISPER_POSTPROCESS_NUM_WORKERS` (default `2`)
- `AUDIO_ASSIST_WHISPER_POSTPROCESS_PARALLELISM` (default `2`)
- `AUDIO_ASSIST_WHISPER_POSTPROCESS_BEAM_SIZE` (default `1`, faster)
- `AUDIO_ASSIST_WHISPER_POSTPROCESS_BEST_OF` (default `1`, faster)
- `AUDIO_ASSIST_WHISPER_POSTPROCESS_VAD_FILTER` (default `true`)
- `AUDIO_ASSIST_WHISPER_POSTPROCESS_NO_SPEECH_THRESHOLD` (default `1.0`, retains low-confidence chunks for recovery)
- `AUDIO_ASSIST_QA_PROVIDER` (`extractive` or `ollama`)
- `AUDIO_ASSIST_OLLAMA_URL`
- `AUDIO_ASSIST_OLLAMA_MODEL`
- `AUDIO_ASSIST_TTS_ENABLED`
- `AUDIO_ASSIST_TTS_SERVICE_URL`
- `AUDIO_ASSIST_TTS_TIMEOUT_SECONDS`
- `AUDIO_ASSIST_TTS_DEFAULT_VOICE`
- `AUDIO_ASSIST_TTS_DEFAULT_LANGUAGE`
- `AUDIO_ASSIST_TTS_DEFAULT_INSTRUCT`
- `AUDIO_ASSIST_MIC_SOURCE_ID` (default `desk-mic`)
- `AUDIO_ASSIST_MIC_SPEAKER_NAME` (default `Todd Deshane`)
- `AUDIO_ASSIST_SPEAKER_ENABLED` (default `false`)
- `AUDIO_ASSIST_SPEAKER_SERVICE_URL` (default `http://127.0.0.1:8791`)
- `AUDIO_ASSIST_SPEAKER_TIMEOUT_SECONDS`
- `AUDIO_ASSIST_SPEAKER_COOLDOWN_SECONDS`
- `AUDIO_ASSIST_SPEAKER_ASYNC_ENRICHMENT` (default `true`, recommended)
- `AUDIO_ASSIST_SPEAKER_QUEUE_SIZE` (default `1024`)
- `AUDIO_ASSIST_SPEAKER_MIN_CONFIDENCE` (default `0.55`)
- `AUDIO_ASSIST_SPEAKER_BACKFILL_ENABLED` (default `true`)
- `AUDIO_ASSIST_SPEAKER_BACKFILL_INTERVAL_SECONDS` (default `20`)
- `AUDIO_ASSIST_SPEAKER_BACKFILL_BATCH_SIZE` (default `24`)
- `AUDIO_ASSIST_SPEAKER_BACKFILL_SINCE_SECONDS` (default `14400`)
- `AUDIO_ASSIST_CALENDAR_MATCH_ENABLED` (default `false`)
- `AUDIO_ASSIST_GOOGLE_SYNC_SERVICE_URL` (default `http://127.0.0.1:8792`)
- `AUDIO_ASSIST_CALENDAR_MATCH_TIMEOUT_SECONDS` (default `2.5`)
- `AUDIO_ASSIST_CALENDAR_MATCH_PADDING_MINUTES` (default `120`)
- `AUDIO_ASSIST_CALENDAR_MATCH_MIN_OVERLAP_SECONDS` (default `180`)
- `AUDIO_ASSIST_CALENDAR_MATCH_MAX_GAP_SECONDS` (default `1800`)
- `AUDIO_ASSIST_GOOGLE_SYNC_AUTOSTART` (default `true`)
- `AUDIO_ASSIST_GOOGLE_SYNC_AUTOSTART_COMMAND` (default `google-sync-service`)
- `AUDIO_ASSIST_GOOGLE_SYNC_AUTOSTART_TIMEOUT_SECONDS` (default `12.0`)
- `AUDIO_ASSIST_GOOGLE_SYNC_AUTOSTART_CWD` (optional)
- `AUDIO_ASSIST_GOOGLE_SYNC_CLIENT_SECRET_PATH` (optional; forwarded to google-sync-service)
- `AUDIO_ASSIST_ARCHIVE_AUDIO`
- `AUDIO_ASSIST_ARCHIVE_AUDIO_DIR`

tts-service env vars:
- `TTS_SERVICE_HOST`
- `TTS_SERVICE_PORT`
- `TTS_SERVICE_PROVIDER` (`qwen3_custom_voice` or `stub`)
- `TTS_SERVICE_STARTUP_LOAD_MODEL`
- `TTS_SERVICE_QWEN_MODEL_ID`
- `TTS_SERVICE_QWEN_DEVICE_MAP`
- `TTS_SERVICE_QWEN_DTYPE`
- `TTS_SERVICE_QWEN_ATTN_IMPLEMENTATION`
- `TTS_SERVICE_QWEN_IMPORT_PATH`
- `TTS_SERVICE_DEFAULT_VOICE`
- `TTS_SERVICE_DEFAULT_LANGUAGE`
- `TTS_SERVICE_DEFAULT_INSTRUCT`

speaker-service env vars:
- `SPEAKER_SERVICE_HOST`
- `SPEAKER_SERVICE_PORT`
- `SPEAKER_SERVICE_WINDOW_SECONDS`
- `SPEAKER_SERVICE_MIN_WINDOW_SECONDS`
- `SPEAKER_SERVICE_MIN_VOICE_DBFS`
- `SPEAKER_SERVICE_SIMILARITY_THRESHOLD`
- `SPEAKER_SERVICE_CREATE_THRESHOLD`
- `SPEAKER_SERVICE_MAX_CLUSTERS_PER_SOURCE`
- `SPEAKER_SERVICE_STALE_SOURCE_SECONDS`

google-sync-service env vars:
- `GOOGLE_SYNC_HOST` (default `127.0.0.1`)
- `GOOGLE_SYNC_PORT` (default `8792`)
- `GOOGLE_SYNC_PUBLIC_BASE_URL` (default `http://127.0.0.1:8792`)
- `GOOGLE_SYNC_OAUTH_REDIRECT_PATH` (default `/v1/auth/callback`)
- `GOOGLE_SYNC_CLIENT_ID`
- `GOOGLE_SYNC_CLIENT_SECRET`
- `GOOGLE_SYNC_CLIENT_SECRET_PATH` (path to Google OAuth JSON)
- `GOOGLE_SYNC_OAUTH_SCOPES` (comma-separated; default Gmail + Calendar readonly)
- `GOOGLE_SYNC_TOKEN_PATH` (default `data/google_sync/tokens.json`)
- `GOOGLE_SYNC_CACHE_PATH` (default `data/google_sync/gmail_latest.json`)
- `GOOGLE_SYNC_CALENDAR_CACHE_PATH` (default `data/google_sync/calendar_latest.json`)
- `GOOGLE_SYNC_DEFAULT_SYNC_MAX_RESULTS`
- `GOOGLE_SYNC_SYNC_MAX_RESULTS_CAP`
- `GOOGLE_SYNC_DEFAULT_LABEL_IDS` (comma-separated; default `INBOX`)
- `GOOGLE_SYNC_DEFAULT_CALENDAR_ID` (default `primary`)
- `GOOGLE_SYNC_DEFAULT_CALENDAR_SYNC_MAX_RESULTS` (default `250`)
- `GOOGLE_SYNC_CALENDAR_SYNC_MAX_RESULTS_CAP` (default `1000`)
- `GOOGLE_SYNC_DEFAULT_CALENDAR_LOOKBACK_HOURS` (default `24`)
- `GOOGLE_SYNC_DEFAULT_CALENDAR_LOOKAHEAD_HOURS` (default `24`)
