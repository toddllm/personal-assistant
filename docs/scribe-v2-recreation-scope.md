# Scribe v2-Style Transcription Scope (Local-First)

## Purpose

Define how we can recreate the core functionality of a modern high-quality transcription product inside this repo, without depending on external transcription APIs.

This is a planning document only. It does not change current runtime behavior.

## Product Goal

Build a local-first transcription stack that supports:

- Always-on capture from mic + system audio + meeting devices.
- Reliable real-time transcript streaming.
- Higher-accuracy post-processing from archived WAV.
- Multilingual transcription with optional translation.
- Speaker-aware timelines (first pass best-effort, improved later).
- Raw transcript preservation (translation/enrichment never overwrites raw).

## Non-Negotiable Principles

1. Capture reliability over model quality.
- Never block recording if ASR/translation/speaker services fail.
- Audio ingestion must keep running even when enrichment services are down.

2. Raw text is immutable.
- Store what ASR heard as `raw_text`.
- Store translation and post-process results as separate fields/versions.

3. Real-time and batch are separate paths.
- Real-time path is latency-optimized.
- Batch/post-process path is accuracy-optimized and can use larger models.

4. Graceful degradation.
- If optional microservices fail (speaker, translation, TTS), core transcript still lands.

## Capability Parity Targets

### A) Real-time transcription

Target:
- Sub-1.5s perceived update cadence in UI.
- Continuous chunked transcript stream with timestamps.

Needed:
- Overlap-aware chunking (already present, continue tuning).
- Source-specific ASR settings (`desk-mic` vs `system-audio`).
- Queue health and dropped-chunk telemetry.

### B) Long-form/batch accuracy

Target:
- Reprocess archived WAV with larger model and better decode settings.
- Replace/append improved text as a second version, not destructive overwrite.

Needed:
- Post-process pipeline on full session audio, not only tiny chunks.
- Word/segment alignment pass for stable timestamps.
- Side-by-side diff tools for QA.

### C) Multilingual handling

Target:
- Correct language retention in raw transcript.
- Optional translation view/API output per chunk/session.

Needed:
- Per-source language hint (already present) + auto detection fallback.
- Language ID stored with each transcript segment.
- Translation as separate enrichment record.

### D) Speaker awareness

Target:
- Per-session stable speaker labels in real-time.
- Optional named speaker mapping (for known local users).

Needed:
- Non-blocking diarization service.
- Backfill relabeling using archived audio.
- Confidence thresholds + `unknown` fallback to avoid bad labels.

### E) Metadata + export

Target:
- Robust transcript session viewer with copy/export.
- Full chronological timeline across sources (already available).

Needed:
- Session-level completeness indicator.
- Export variants: raw only, raw+translation, with/without speaker tags.

## Proposed Architecture (Microservice-Friendly)

Current services are a good base:
- `audio-assist` (capture + real-time ASR + query + storage)
- `speaker-service` (optional diarization)
- `tts-service` (optional synthesis)

Add/expand internal modules (can remain inside `audio-assist` initially):

1. `ingest pipeline`
- Device capture + ffmpeg + archive writer.
- First guarantee: audio chunk persisted.

2. `realtime-asr pipeline`
- Fast model/profile for low latency.
- Writes transcript segments quickly.

3. `batch-asr pipeline`
- Reads archived WAV session windows.
- Uses larger model and slower decode settings.
- Produces improved transcript versions.

4. `language enrichment`
- Detect spoken language per segment/session.
- Route optional translation jobs.

5. `speaker enrichment`
- Async diarization + optional enrollment matching.
- Backfill labels over prior segments.

6. `serving/query layer`
- Live transcript endpoint + full transcript manager + search/Q&A grounding.

## Data Model Direction

Keep existing transcript rows, extend with versioning/enrichment tables.

Suggested logical shape:

- `transcript_segments`
  - `id`, `source_id`, `session_id`, `started_at`, `ended_at`
  - `raw_text` (immutable)
  - `raw_language` (detected or hinted)
  - `asr_pass` (`realtime` | `batch_v1` | `batch_v2`)
  - `confidence_summary`
  - `speaker_label` (nullable)

- `transcript_enrichments`
  - `segment_id`, `kind` (`translation`, `speaker`, `keywords`, `entities`)
  - `target_language` (for translation)
  - `text` / `json_payload`
  - `model_id`, `created_at`, `version`

- `session_metrics`
  - ingest gaps, queue depth max, dropped chunk count, processing lag

## Phased Build Plan

### Phase 0: Stability baseline (now)

- Keep current real-time capture stable.
- Add explicit ingest/transcribe lag metrics to API/UI.
- Ensure transcript UI always shows raw by default.

Exit criteria:
- No data loss when optional services are off/down.
- Clear status telemetry for queue pressure and dropped chunks.

### Phase 1: Real-time quality upgrades

- Source-specific decode profiles:
  - `desk-mic`: tighter VAD, conversational speech tuning.
  - `system-audio`: lower no-speech filtering, better recall.
- Dynamic chunk duration based on speech density.
- Better boundary stitching to reduce clipped words.

Exit criteria:
- Better recall on noisy/media audio with no regression for mic capture.

### Phase 2: Strong post-process pipeline

- Session-level batch transcription from archived WAV.
- Larger model default for post-process (already defaulting larger model; extend to session windows).
- Alignment pass for improved timestamps.
- Non-destructive apply workflow (`preview` then `apply`).

Exit criteria:
- Measurable WER/CER reduction vs realtime pass on multilingual samples.

### Phase 3: Multilingual-first workflow

- Improve language ID reliability per segment/session.
- Keep raw transcript in original language.
- Add translation toggles in UI/API without replacing raw text.

Exit criteria:
- Spanish-heavy system audio remains mostly Spanish in `raw_text`.
- Translation output available on demand.

### Phase 4: Speaker quality path

- Strengthen diarization model options (optional GPU path).
- Add stable session speaker tracking + confidence.
- Optional enrollment mapping for known voices (for named labels).

Exit criteria:
- Stable label continuity in meetings/interviews with minimal false swaps.

### Phase 5: “Product polish” parity

- Better transcript search/filtering and confidence highlighting.
- Segment health flags (music-heavy, low confidence, overlapping speakers).
- One-click export bundles for note workflows.

## Acceptance Metrics

Track per source type and language.

Core metrics:
- Ingest continuity: `% time recording active with no dropped audio writes`.
- ASR latency: P50/P95 time from chunk end to transcript persisted.
- Recall proxy: voiced seconds vs transcribed seconds.
- Text quality: WER/CER on curated evaluation sets.
- Language accuracy: language ID correctness per segment.
- Diarization quality: DER/JER (when speaker service enabled).

Quality gates:
- No release if ingest continuity regresses.
- No release if mic realtime latency regresses > agreed threshold.

## Evaluation Dataset Plan

Create a local benchmark pack from real usage:

- `desk-mic` English speech.
- `system-audio` sports commentary.
- `system-audio` Spanish music/lyrics.
- Mixed-language clips (Spanish/English code-switch).
- Overlap clips (mic + background media).

For each clip:
- Keep raw WAV.
- Keep human-corrected reference transcript for scoring.
- Record expected language and speaker notes.

## Risks and Mitigations

Risk: Better models increase latency/compute cost.
- Mitigation: keep strict split between realtime and batch pipelines.

Risk: Over-aggressive filtering drops content.
- Mitigation: source-specific thresholds + fallback decode path + telemetry.

Risk: Speaker labeling harms trust if wrong.
- Mitigation: confidence thresholds, `unknown` fallback, async backfill.

Risk: Translation contaminates raw text.
- Mitigation: enforce schema separation (`raw_text` immutable).

## Out of Scope (for now)

- Celebrity identity recognition from public voiceprints.
- Cross-session global speaker identity guarantees.
- Fully automated factual correction of ASR output.
- Replacing current stable capture path with experimental code.

## Implementation Notes for This Repo

- Keep current services and APIs backward compatible.
- Add features behind config flags where possible.
- Prefer additive schema changes and migration scripts.
- Preserve current user-critical behavior:
  - one transcript per source session for transcript manager view
  - interleaved timeline with timestamps
  - optional services never block capture

## Suggested Next Step When We Start

Start with a dedicated "accuracy harness":
- one command to run realtime pass + batch pass on archived session audio,
- compare WER/CER + latency + language detection,
- produce a markdown report in `docs/benchmarks/`.

That gives objective signal for each improvement before changing defaults.
