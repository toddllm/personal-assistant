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
```

UI defaults:
- common source presets are pre-populated (`desk-mic` + detected virtual loopback devices such as BlackHole/Loopback/Soundflower)
- `Add Source` starts selected preset
- `Auto-start common sources` starts default presets on page load
- voice playback supports chunked streaming (`Stream Voice` toggle)
- `/transcripts` provides paginated full-history transcript browsing with copy/download tools
- transcript APIs now default to session view (one growing transcript per capture session)
- default mic speaker label is `Todd Deshane` for source `desk-mic`
- transcript page defaults to interleaved timeline mode with timestamps; enable `Session view` toggle when needed
- speaker labeling is optional and best-effort; transcription continues if speaker service is slow/unavailable

`audio-assist` defaults:
- host/port: `127.0.0.1:8787`
- transcript DB: `data/audio_assist.db`
- archived audio segments: `data/audio_segments/`
- whisper model: `base.en` (lazy-loaded on first audio segment)
- TTS service URL: `http://toddllm:8790`
- speaker service URL: `http://127.0.0.1:8791` (disabled by default)

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

## 4) Start listening

Mic source:

```bash
curl -sS -X POST http://127.0.0.1:8787/v1/sources/start \
  -H "Content-Type: application/json" \
  -d '{"source_type":"mic","source_id":"desk-mic"}'
```

Meeting/podcast via ffmpeg:

```bash
curl -sS -X POST http://127.0.0.1:8787/v1/sources/start \
  -H "Content-Type: application/json" \
  -d '{
    "source_type":"ffmpeg",
    "source_id":"system-audio",
    "ffmpeg_input":":0",
    "ffmpeg_input_format":"avfoundation"
  }'
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
- `GET /v1/devices/mic`
- `GET /v1/ollama/models`
- `GET /v1/tts/status`
- `GET /v1/tts/voices`
- `GET /v1/speaker/status`
- `POST /v1/tts/synthesize/stream` (SSE chunked voice stream)
- `GET /v1/sources`
- `POST /v1/sources/start`
- `POST /v1/sources/stop/{source_id}`
- `GET /v1/transcripts/recent`
- `GET /v1/transcripts/page`
- `GET /v1/transcripts/sources`
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

## 7) Config

audio-assist env vars:
- `AUDIO_ASSIST_HOST`
- `AUDIO_ASSIST_PORT`
- `AUDIO_ASSIST_SAMPLE_RATE`
- `AUDIO_ASSIST_SEGMENT_SECONDS`
- `AUDIO_ASSIST_WHISPER_MODEL`
- `AUDIO_ASSIST_WHISPER_DEVICE`
- `AUDIO_ASSIST_WHISPER_COMPUTE_TYPE`
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
