# Audio Pipeline Resource Analysis

**Date:** 2026-03-02 (updated from 2026-03-01 initial report)
**Scope:** Full offline code + data analysis of audio-assist, audio-forward, mic-forward

## Executive Summary

The `audio-assist` service dominates resource usage: **~50% CPU (bursty), 353 MB RSS, 36 threads** after 3 days of uptime. The audio-forward and mic-forward processes are lightweight (~0.1% CPU, ~30 MB each) but collectively spawn **~180 subprocesses/minute** for device polling.

Log data shows the pipeline is **operationally healthy** — queue backlog is rare (4.6% of checks), processing rate is stable at ~27 segments/minute, and no degradation over 76 hours. The performance cost is high but not worsening.

### Key Numbers

| Metric | Value |
|--------|-------|
| Segments processed (3 days) | 165,400 |
| Segments with speech | 21.4% (35,570) |
| Silent segments | 78.6% (129,830) |
| Queue backlog events | 4.6% of checks (almost always 0) |
| Subprocess spawn rate (all processes) | ~3/sec (~180/min) |
| Device stall events | ~3.3/day (Bluetooth) |
| MLX Whisper model | medium, cached via ModelHolder (confirmed) |
| Fallback transcription rate | Not tracked — needs instrumentation |

---

## Part 1: Where CPU Goes

### 1.1 Whisper MLX Inference — bursty 10–15% per segment

Each 4.5-second audio segment triggers `mlx_whisper.transcribe()` which runs the Whisper medium model on the Apple GPU via Metal. The model is **cached** — confirmed by reading `mlx_whisper/transcribe.py:50-59` (`ModelHolder` class singleton). No reload per call.

With 2 active sources (desk-mic + app-audio), segments arrive every ~2 seconds. Each inference spike averages the CPU to ~50% over long `ps` sampling windows.

### 1.2 Wasted Work on Silent Segments — 78.6% of all processing

The transcriber's `_transcribe()` method (`transcriber.py:345-398`) processes every segment:

```
segment dequeued → np.frombuffer(pcm, int16) → .astype(float32) / 32768.0
  → reshape + mean (if stereo) → _signal_dbfs() check → return "" if silent
```

78.6% of segments hit the early-return path. Each still allocates a ~140 KB numpy float32 array that's immediately discarded. At 27 segments/minute, that's ~21 wasted numpy allocations/minute.

**Worse:** when the primary transcription returns empty text, `_fallback_on_empty` triggers a **second inference call** with `beam_size=2, best_of=2` (4× slower than primary). The fallback dBFS threshold is separate and more permissive, so segments that barely pass get double-processed.

### 1.3 Meeting Detection — 10–15 osascript subprocesses every 8 seconds

The orchestrator (`orchestrator.py:119`) polls `detector.detect()` every 8 seconds. Each `detect()` sequentially calls 4 check functions:

| Check | osascript calls | What it queries |
|-------|----------------|-----------------|
| `_check_zoom` | 3 | Process list, window names, menu bar |
| `_check_teams` | 3–4 | 3 Teams process variants × window query |
| `_check_browser_meet` | 4–5 | Chrome, Arc, Edge, Brave tab queries |
| `_check_webex` | 2 | Process list, window names |

**Total: 10–15 osascript processes spawned every 8 seconds.** Each has 3-second timeout. If all browsers are running, worst-case latency per poll is 3–10+ seconds from cascading subprocess waits.

These checks are **not batched** — each provider queries `name of every process` independently instead of sharing a single process list.

### 1.4 Device Watchdog Subprocesses — ~180/minute across scripts

Both `audio-forward.py` and `mic-forward.py` spawn fresh Python interpreter subprocesses for device checks:

| Script | Pattern | Frequency | Cost per call |
|--------|---------|-----------|---------------|
| audio-forward | Per-device watchdog | Every 2s × N output devices | ~100–200ms Python startup |
| audio-forward | Discovery thread | Every 5s | ~100–200ms |
| mic-forward | Per-mic watchdog | Every 2s × M input devices | ~100–200ms |
| mic-forward | Discovery thread | Every 5s | ~100–200ms |

With 2 output devices + 3 input mics: **2.5 subprocesses/sec from watchdogs + 0.4/sec from discovery = ~3 subprocesses/sec = 180/minute.**

Each subprocess runs `import sounddevice; sd.query_devices()` — a full PortAudio initialization cycle.

**Note:** `volume-control.py` uses native CoreAudio bindings (`coreaudiod` via ctypes) with **zero subprocess spawning**. This proves the approach works and could be adopted by the other scripts.

---

## Part 2: Where Memory Goes (353 MB RSS)

| Component | Estimated | Notes |
|-----------|-----------|-------|
| MLX Whisper medium weights | ~200 MB | Resident in unified memory (shared CPU/GPU) |
| Python interpreter + libraries | ~40 MB | numpy, sounddevice, mlx, fastapi, speechbrain |
| ECAPA speaker embedding model | ~30 MB | Loaded at startup, used intermittently |
| Thread stacks (36 threads) | ~36 MB | 1 MB default per thread |
| MLX ThreadPool (4 workers + stream) | ~20 MB | Parked but resident |
| FFmpeg reader buffers (2 sources) | ~10 MB | PCM bytearray per source |
| CoreAudio HAL + IO threads | ~8 MB | |
| Other (caches, counters, GC) | ~9 MB | |

The transcription queue (capacity 1024) and speaker queue (capacity 1024) are **almost always empty** (95.4% of checks show queue=0). However, under backlog they could each hold up to ~140 MB (1024 × 140 KB per segment). This is an unreasonable worst-case allocation for a pipeline that rarely exceeds queue depth of 2.

### Buffer Management Issues

**sources.py:142-144** — The segment drain loop does:
```python
chunk = bytes(self._buffer[: self._segment_bytes])   # 140 KB copy
del self._buffer[: self._segment_step_bytes]          # O(n) memmove of remainder
```

Both operations are wasteful: `bytes()` creates an unnecessary copy of data already in `bytearray`, and `del buffer[0:N]` shifts the entire remaining buffer. A read-index approach would eliminate both.

---

## Part 3: Log Data Analysis (76 hours observed)

### Processing Rate — Stable, No Degradation

| Window | Rate |
|--------|------|
| Feb 27 early | 0.52 segments/sec |
| Feb 28 midday | variable (brief backlog periods) |
| Mar 2 afternoon | 0.83 segments/sec |

Processing throughput is **stable or slightly improving** — no evidence of degradation over time.

### Queue Health — Excellent

| Queue Depth | Occurrences | % of Checks |
|-------------|-------------|-------------|
| 0 | 2,376 | 95.4% |
| 1 | 110 | 4.4% |
| 2 | 4 | 0.16% |
| 3 | 1 | 0.04% |

The pipeline never backs up meaningfully.

### Device Stall Events — Recurring Pattern

- **50 "device disappeared" events** over 15 days (~3.3/day)
- **59 PortAudio reinit events** (correlates with disconnects)
- Primary problem device: Bose QC45 (Bluetooth)
- Pattern: disconnect → 5–40 minute gap → reconnect → sometimes silently stalls
- Fix already applied: 30-minute stream cycling + `stream.active` check

### Database Growth

- `audio_assist.db`: 74 MB after 3+ weeks
- Stable growth at expected rate, no bloat

---

## Part 4: Redundant Work in Service Layer

### 4.1 `ensure_capture_sources_running()` called 3–5× per watchdog cycle

This expensive function (calls `source_manager.statuses()` + `transcriber.transcription_pipeline_status()`) is invoked from:

1. Startup (`service.py:1270`)
2. Capture watchdog loop every 5s (`service.py:1167`)
3. Session start callback (`service.py:1242`)
4. Readiness endpoint (`service.py:1582`)
5. Repeatedly during PREPARING state (`orchestrator.py:161-179`)

These overlap — the watchdog and orchestrator both call it on their own timers.

### 4.2 Thread-per-request for device probing

`/v1/devices/mic/levels` (`service.py:1921-1955`) spawns a **new thread per input device** to probe levels. With 10 devices: 10 threads created/destroyed per API call, blocking for 450ms+ each. No thread pool.

### 4.3 Volume read spawns 3+ subprocesses

`GET /v1/volume` (`service.py:2436-2482`) runs 3 subprocess calls to SwitchAudioSource + osascript per request. No caching.

---

## Prioritized Recommendations

### Tier 1 — High Impact, Low Risk

| # | Change | CPU Savings | Memory Savings | Effort |
|---|--------|-------------|----------------|--------|
| 1 | **Compute dBFS on raw int16 PCM before numpy conversion** in `transcriber.py:349`. RMS of int16 is trivial: `np.frombuffer(pcm, np.int16)` then `20*log10(rms/32768)`. Skip the float32 conversion + reshape for 78% of segments. | ~5–10% | ~2 MB/min GC pressure | Small |
| 2 | **Reduce queue sizes** `transcription_queue_size` and `speaker_queue_size` from 1024 → 128 in `config.py:45,105`. Queue is >95% empty. | — | Reduces worst-case by 250 MB | Trivial |
| 3 | **Batch osascript calls in meeting detector**. Query `name of every process` once, share result across all 4 check functions. Eliminate 60–70% of subprocess spawns. | ~30–50% of meeting detection overhead | — | Medium |
| 4 | **Consolidate device watchdog into single polling thread** per script. Instead of N watchdog threads each spawning subprocesses every 2s, one thread polls all devices every 5s. | Eliminates ~150 subprocesses/min | — | Medium |
| 5 | **Don't enqueue full AudioSegment in speaker queue** (`transcriber.py:564-576`). Store only metadata (transcript_id, source_id, timestamps). Load audio on-demand from archive when enrichment runs. | — | Eliminates 140 MB worst-case | Medium |

### Tier 2 — Medium Impact

| # | Change | Impact | Effort |
|---|--------|--------|--------|
| 6 | **Use read-index for segment buffer drain** in `sources.py:142-144`. Replace `del buffer[0:N]` (O(n) memmove) with offset tracking + periodic compaction. | 3–8% CPU reduction | Small |
| 7 | **Increase meeting detection poll interval** from 8s → 20–30s when no meeting detected. Keep 8s only during active meetings. | 60–75% fewer osascript spawns when idle | Small |
| 8 | **Adopt volume-control's native CoreAudio approach** for device discovery in audio-forward and mic-forward. Eliminates all Python subprocess spawning for device checks. | Eliminates 180 subprocesses/min | Large |
| 9 | **Add StreamingResampler to mic-forward**. Currently uses stateless `resample()` (`mic-forward.py:308-320`) that recalculates indices every block with no fractional sample tracking — causes drift/crackling. Copy the `StreamingResampler` class from audio-forward. | Fixes audio quality bug | Small |
| 10 | **Tune fallback transcription**. Set `whisper_fallback_beam_size=1` and `whisper_fallback_best_of=1` (currently 2/2). The 4× compute penalty for empty-text fallback segments is not justified. | 5–20% CPU when fallback fires | Trivial |

### Tier 3 — Architectural

| # | Change | Impact | Effort |
|---|--------|--------|--------|
| 11 | Switch to `whisper-small` when no meeting active, `medium` during meetings | 50% CPU + 100 MB memory when idle | Medium |
| 12 | Replace Python audio-forward with Rust version (`audio-forward-rs`) | Eliminates 30 MB Python runtime + all subprocess polling | Large |
| 13 | Deduplicate `ensure_capture_sources_running()` calls with a debounce/cache (call at most once per 5s) | Reduces redundant expensive checks | Small |
| 14 | Use `ThreadPoolExecutor(max_workers=4)` for device probe endpoint instead of thread-per-device | Reduces thread churn on API calls | Small |

---

## Confirmed Non-Issues

- **MLX model reload**: `ModelHolder` caches the model singleton. No reload per transcription call. ✓
- **Processing degradation over time**: Log analysis shows stable throughput across 76 hours. ✓
- **Queue backlog**: 95.4% of checks show depth=0. Pipeline keeps up fine. ✓
- **Database bloat**: 74 MB after 3 weeks is normal growth rate. ✓
