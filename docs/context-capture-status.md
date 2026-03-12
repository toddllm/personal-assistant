# Better Context Capture — Project Status

> Last updated: 2026-02-13
> Repo: `toddllm/personal-assistant` (branch: `main`)

---

## Quick Start for Next Agent

**Read these files first to understand the project:**
1. `src/audio_assist/config.py` — Pydantic settings, all config lives here
2. `src/audio_assist/transcriber.py` — Whisper transcription + speaker attribution pipeline
3. `src/audio_assist/diarization.py` — Current speaker service client (HTTP to port 8791)
4. `src/audio_assist/sources.py` — Audio capture (MicrophoneSourceRunner, FFmpegSourceRunner)
5. `src/audio_assist/storage.py` — SQLite transcript store (TranscriptStore class)

**Read these research docs for decisions already made:**
1. `docs/speaker-embedding-model-comparison.md` — Issue 1.1 deliverable (416 lines)
2. `docs/screen-capture-approach-comparison.md` — Issue 2.1 deliverable (606 lines)

**Read these existing planning docs for architectural context:**
1. `docs/openclaw-inspired-microservices-plan.md` — 7-service target architecture
2. `docs/scribe-v2-recreation-scope.md` — Transcription quality targets, 6-phase build plan

---

## Current Services

| Service | Port | Status |
|---------|------|--------|
| audio-assist | 8787 | Running (monolith) |
| tts | 8790 | Running |
| speaker | 8791 | Running (32-band spectral, to be replaced) |
| google-sync | 8792 | Running |
| email | 8793 | Running |
| screen-capture | 8794 | **Not yet built** (Epic 2) |

---

## What's Done

### GitHub Project Infrastructure
- **13 labels** created: `priority:high`, `priority:medium`, `priority:medium-low`, `research`, `design`, `implementation`, `testing`, `approval-gate`, `speaker-id`, `screen-capture`, `diarization`, `microservices`, `meeting-notes`
- **6 milestones** created (Phase 1 through Phase 6, Feb 27 → Jun 5)
- **5 epic issues** (#3–#7) with linked sub-issue checklists
- **29 sub-issues** (#8–#36) with full descriptions, acceptance criteria, and dependency chains

### Issue 1.1 — Research & Select Speaker Embedding Model ✅
- **Deliverable:** `docs/speaker-embedding-model-comparison.md`
- **Decision: SpeechBrain ECAPA-TDNN** (`speechbrain/spkrec-ecapa-voxceleb`)
  - 0.80% EER on VoxCeleb1-O (best of 4 candidates)
  - 192-dimensional embeddings, 83.3 MB model
  - Apache 2.0 license, no gated access
  - Estimated 30–80ms per segment on Apple Silicon CPU
  - Default cosine similarity threshold: 0.25
  - Robust to compressed audio (training includes compression augmentation)
- **Close this issue on GitHub** — deliverable is complete

### Issue 2.1 — Research Screen Capture Approaches on macOS ✅
- **Deliverable:** `docs/screen-capture-approach-comparison.md`
- **Decision: `CGWindowListCreateImage` via PyObjC** (with `mss` fallback)
  - 26ms window-specific capture at Retina resolution
  - Full pipeline (capture + 50% resize + WebP q50 m0): 156ms, ~95 KB/frame
  - 1.04% CPU duty cycle at 15-second intervals
  - PyObjC 10.3.1 already installed in project
  - Deprecation risk managed via `ScreenCapturer` protocol abstraction
  - macOS Screen Recording permission required (monthly re-auth on Sequoia)
  - Zoom window detection: filter `CGWindowListCopyWindowInfo` by `kCGWindowOwnerName == "zoom.us"`
- **Close this issue on GitHub** — deliverable is complete

---

## What's Next (Immediate)

### Issue 1.2 — Design Voice Enrollment API and Storage Schema
- **GitHub:** #9 | **Milestone:** Phase 1 | **Priority:** HIGH
- **Depends on:** Issue 1.1 (done — model selected, embedding_dim = 192)
- **Deliverables:**
  - OpenAPI spec at `docs/api-specs/speaker-enrollment.yaml`
  - Schema migration SQL at `migrations/001_speaker_profiles.sql`
  - Updated `config.py` with new fields (disabled by default)
- **Key design from issue body:**
  - API: `POST /v1/enroll`, `GET /v1/profiles`, `DELETE /v1/profiles/{id}`, `POST /v1/verify`
  - SQLite table `speaker_profiles`: id, name, embedding_blob, embedding_dim (192), enrolled_at, sample_count, is_owner
  - Config: `owner_speaker_profile_id`, `speaker_embedding_model`, `speaker_similarity_threshold: float = 0.75`, `speaker_verification_enabled: bool = False`
  - Note: threshold in issue body says 0.75, but research doc recommends starting at 0.25 (SpeechBrain default). Reconcile during design.

### Issue 2.2 — Design Screen-Capture Microservice API and Storage
- **GitHub:** #15 | **Milestone:** Phase 1 | **Priority:** HIGH
- **Depends on:** Issue 2.1 (done — approach selected)
- **Deliverables:**
  - OpenAPI spec at `docs/api-specs/screen-capture.yaml`
  - Schema migration SQL at `migrations/002_screen_captures.sql`
  - Config additions to `config.py`
- **Key design from issue body:**
  - New FastAPI service on port 8794, binding 127.0.0.1
  - API: `POST /v1/capture`, `GET /v1/captures/{session_id}`, `GET /v1/captures/latest`, etc.
  - Storage: `data/screen_captures/{session_id}/{timestamp}.webp`
  - SQLite table `screen_captures`: id, session_id, captured_at, file_path, width, height, file_size_bytes, ocr_text, participants_detected
  - Config: `screen_capture_enabled: bool = False`, `screen_capture_interval_seconds: int = 15`, `screen_capture_quality: int = 60`, etc.

**These two issues can be done in parallel. They have no dependency on each other.**

---

## Full Issue Map

### Epic 1: Voice Enrollment & Speaker ID (#3) — HIGH priority
| # | Issue | Status | Milestone | Depends on |
|---|-------|--------|-----------|------------|
| #8 | 1.1 Research & select speaker embedding model | ✅ DONE | Phase 1 | — |
| #9 | 1.2 Design voice enrollment API and storage schema | **NEXT** | Phase 1 | 1.1 |
| #10 | 1.3 Implement enrollment CLI / workflow | Pending | Phase 2 | 1.1, 1.2 |
| #11 | 1.4 Implement real-time speaker verification in pipeline | Pending | Phase 2 | 1.1, 1.2, 1.3 |
| #12 | 1.5 Backfill existing transcripts with voice ID | Pending | Phase 3 | 1.3, 1.4 |
| #13 | 1.6 Human approval gate: validate accuracy | Pending | Phase 3 | 1.4, 1.5 |

### Epic 2: Screen Capture Microservice (#4) — HIGH priority
| # | Issue | Status | Milestone | Depends on |
|---|-------|--------|-----------|------------|
| #14 | 2.1 Research screen capture approaches on macOS | ✅ DONE | Phase 1 | — |
| #15 | 2.2 Design screen-capture microservice API and storage | **NEXT** | Phase 1 | 2.1 |
| #16 | 2.3 Implement basic screen capture service | Pending | Phase 2 | 2.1, 2.2 |
| #17 | 2.4 Implement OCR / visual context extraction | Pending | Phase 3 | 2.3 |
| #18 | 2.5 Implement Zoom participant name extraction | Pending | Phase 3 | 2.4 |
| #19 | 2.6 Integrate visual context into transcript enrichment | Pending | Phase 4 | 2.4, 2.5 |
| #20 | 2.7 Privacy controls and human approval gate | Pending | Phase 4 | 2.3, 2.4 |

### Epic 3: Production-Grade Speaker Diarization (#5) — MEDIUM priority
| # | Issue | Status | Milestone | Depends on |
|---|-------|--------|-----------|------------|
| #21 | 3.1 Integrate neural model for multi-speaker diarization | Pending | Phase 4 | Epic 1 (1.1–1.4) |
| #22 | 3.2 Design cross-session speaker identity store | Pending | Phase 5 | 3.1 |
| #23 | 3.3 Implement speaker naming workflow | Pending | Phase 5 | 3.2 |
| #24 | 3.4 Combine voice ID + visual context for attribution | Pending | Phase 5 | Epic 2 (2.5), 3.2, 3.3 |
| #25 | 3.5 Testing & human approval gate | Pending | Phase 5 | 3.1–3.4 |

### Epic 4: Microservices Decomposition (#6) — MEDIUM priority
| # | Issue | Status | Milestone | Depends on |
|---|-------|--------|-----------|------------|
| #26 | 4.1 Extract audio-capture as standalone microservice | Pending | Phase 5 | — |
| #27 | 4.2 Extract ASR (transcription) as standalone microservice | Pending | Phase 5 | — |
| #28 | 4.3 Extract transcript-store as standalone microservice | Pending | Phase 6 | — |
| #29 | 4.4 Implement service discovery and health aggregation | Pending | Phase 6 | — |
| #30 | 4.5 Define inter-service contracts (OpenAPI specs) | Pending | Phase 6 | 4.1–4.3 |
| #31 | 4.6 Human approval gate | Pending | Phase 6 | 4.1–4.5 |

### Epic 5: Meeting Notes Quality & Automation (#7) — MEDIUM-LOW priority
| # | Issue | Status | Milestone | Depends on |
|---|-------|--------|-----------|------------|
| #32 | 5.1 Design meeting notes generation pipeline | Pending | Phase 6 | Epics 1–2 |
| #33 | 5.2 Implement automated post-meeting processing | Pending | Phase 6 | 5.1 |
| #34 | 5.3 Implement meeting notes review/approval workflow | Pending | Phase 6 | 5.2 |
| #35 | 5.4 Integrate Zoom chat log ingestion | Pending | Phase 6 | — |
| #36 | 5.5 Testing & human approval gate | Pending | Phase 6 | 5.1–5.4 |

---

## Key Architecture Decisions (Already Made)

### Speaker Embedding
- **Model:** `speechbrain/spkrec-ecapa-voxceleb` (ECAPA-TDNN)
- **Embedding dim:** 192
- **Device:** CPU (avoid MPS for reliability)
- **Threshold:** Start at 0.25 (SpeechBrain default), tune empirically
- **Integration:** Replace `_owner_speaker_for_segment()` in `transcriber.py:524-527`
- **Current hardcoded attribution:** `config.py:76` → `mic_speaker_name: str = "Todd Deshane"`

### Screen Capture
- **Primary:** `CGWindowListCreateImage` via PyObjC (Quartz framework)
- **Fallback:** `mss` for full-screen capture
- **Format:** WebP, quality 50, 50% scale from Retina
- **Interval:** 15 seconds during active meetings
- **Port:** 8794 (new standalone FastAPI service)
- **Zoom detection:** `CGWindowListCopyWindowInfo` filtered by `kCGWindowOwnerName == "zoom.us"`

### Architectural Principles (from existing docs)
- Loopback-first (127.0.0.1) security
- Raw transcript immutable, enrichments in separate fields/tables
- Graceful degradation — enrichment never blocks capture
- New features disabled by default (`*_enabled: bool = False`)
- Pydantic config with `AUDIO_ASSIST_` env prefix
- Async enrichment that never blocks the capture pipeline
- SQLite storage with migration scripts

---

## Key Files to Modify

### For Issue 1.2 (Voice Enrollment API Design)
- **Create:** `docs/api-specs/speaker-enrollment.yaml`
- **Create:** `migrations/001_speaker_profiles.sql`
- **Modify:** `src/audio_assist/config.py` — add speaker verification config fields

### For Issue 2.2 (Screen Capture API Design)
- **Create:** `docs/api-specs/screen-capture.yaml`
- **Create:** `migrations/002_screen_captures.sql`
- **Modify:** `src/audio_assist/config.py` — add screen capture config fields

### For Issue 1.3 (Enrollment CLI — Phase 2)
- **Create:** `src/audio_assist/enroll.py`
- **Create:** `tests/test_enroll.py`

### For Issue 2.3 (Screen Capture Service — Phase 2)
- **Create:** `src/screen_capture/` (entire new service package)
- **Create:** `scripts/screen-capture-service.sh`
- **Create:** `tests/test_screen_capture.py`

---

## Existing Codebase Key Points

### TranscriptionWorker (transcriber.py)
- `_owner_speaker_for_segment()` at line 524 — **the method to replace with voice verification**
- Currently: if `source_id == mic_source_id` and `mic_speaker_name` is set → return name; else None
- Speaker enrichment already runs async via `_run_speaker_enrichment()` thread
- Backfill loop already exists at `_run_speaker_backfill_loop()`

### SpeakerDiarizationClient (diarization.py)
- HTTP client to external speaker service at port 8791
- `POST /v1/diarize/chunk` with base64-encoded PCM audio
- Returns `SpeakerDetection(speaker, confidence)`
- Has cooldown/retry logic and health probing

### SourceManager (sources.py)
- `MicrophoneSourceRunner` — sounddevice-based mic capture
- `FFmpegSourceRunner` — ffmpeg subprocess for system audio (BlackHole 2ch)
- Both produce `AudioSegment` objects with PCM16LE audio

### TranscriptStore (storage.py)
- SQLite with `transcripts` table (id, source_id, session_id, started_at, ended_at, text, speaker)
- Full-text search via `transcripts_fts` (FTS5)
- `transcript_sessions` table for session metadata
- `update_speaker()` method for async backfill

### Config (config.py)
- All settings via Pydantic `BaseSettings` with `AUDIO_ASSIST_` env prefix
- Key current settings: `mic_speaker_name: str = "Todd Deshane"`, `speaker_enabled: bool = False`
- Pattern: new features always default to disabled

---

## Timeline

```
Phase 1 (Weeks 1-2, due Feb 27):  1.1✅ 1.2⬜ 2.1✅ 2.2⬜     ← YOU ARE HERE
Phase 2 (Weeks 3-4, due Mar 13):  1.3   1.4   2.3
Phase 3 (Weeks 5-6, due Mar 27):  1.5   1.6   2.4   2.5
Phase 4 (Weeks 7-8, due Apr 10):  2.6   2.7   3.1
Phase 5 (Weeks 9-12, due May 8):  3.2   3.3   3.4   3.5   4.1   4.2
Phase 6 (Weeks 13-16, due Jun 5): 4.3   4.4   4.5   4.6   5.1-5.5
```

---

## How to Close Completed Issues

Issues 1.1 (#8) and 2.1 (#14) have their deliverables complete but haven't been closed on GitHub yet. Close them with:

```bash
gh issue close 8 --comment "Deliverable: docs/speaker-embedding-model-comparison.md. Recommendation: SpeechBrain ECAPA-TDNN."
gh issue close 14 --comment "Deliverable: docs/screen-capture-approach-comparison.md. Recommendation: CGWindowListCreateImage via PyObjC."
```
