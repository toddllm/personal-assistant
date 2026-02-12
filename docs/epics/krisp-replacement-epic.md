# EPIC: Replace Krisp.ai With Local-First Meeting Capture

## Summary
Build a production-grade, local-first meeting capture stack that reliably does the practical Krisp workflow:

1. Noise cancellation.
2. Automatic meeting recording/transcription based on meeting metadata (Zoom/Browser/etc.).
3. Clean separation of mic voice vs system/remote voices.
4. Fast copy/export of raw transcript for downstream AI workflows.

## Why
Current `audio-assist` is a useful POC but still suboptimal for daily high-stakes usage:
- System audio can go silent/stale.
- Capture treats too many inputs as equivalent mic paths.
- Session lifecycle is not yet as deterministic as Krisp-style meeting capture.
- Quality gates are not strict enough to prevent missed windows.

## Product Principle
Optimize for reliability and simple operator experience over experimental flexibility:
- Always capture something useful during a real meeting.
- Preserve the current imperfect but working mic/system capture path as fallback.
- Prefer deterministic defaults, explicit health signals, and recoverable failure modes.

## Success Metrics
- Auto-start/stop accuracy for meeting sessions: `>= 98%` on accepted metadata events.
- Missed critical meeting windows: `0` over a rolling 14-day burn-in.
- System-audio capture availability during active meetings: `>= 99%` checks healthy.
- Transcript availability SLA: first transcript chunk visible within `<= 20s` of session start.
- Export workflow: raw transcript copy/download available in `<= 2 clicks`.

## Scope
- Audio capture architecture and service lifecycle.
- Meeting metadata ingestion + session orchestration.
- Noise cancellation pipeline and quality measurement.
- Mic/system voice separation and transcript channel labeling.
- UI simplification and defaults for daily usage.
- Observability, SLOs, and incident runbooks.

## Out of Scope
- Cloud multi-tenant SaaS.
- Perfect speaker diarization for every edge case in this epic.
- Cross-platform parity beyond macOS.

## Architecture Direction (Proposed)
Hybrid model (recommended):
- Native macOS companion app for deterministic audio device/control plane.
- Existing local services remain the transcript/data plane.
- Browser/service connectors provide meeting metadata triggers.

Rationale:
- Krisp-level behavior on macOS is easier with explicit device/control ownership.
- Browser metadata alone is insufficient for robust audio path management.
- Preserves local-first constraints and current service investments.

## Workstreams

### WS1: Capture Core v2 (Local Audio Plane)
- [ ] Define canonical source model: `mic`, `system`, `meeting_mix`, `fallback`.
- [ ] Add explicit source role semantics (not just `source_type=mic`).
- [ ] Implement deterministic device selection policy with pinned defaults + fallback order.
- [ ] Preserve current POC path as fallback capture mode.

### WS2: Metadata-Driven Session Orchestrator
- [ ] Build metadata adapters (Zoom/browser meeting titles, URLs, active-call state).
- [ ] Add orchestrator state machine: `idle -> preparing -> recording -> cooldown -> idle`.
- [ ] Add preflight checks before record (mic signal, system route signal, disk, queue health).
- [ ] Auto-open/close transcript sessions based on metadata + guard timers.

### WS3: Noise Cancellation + Voice Separation
- [ ] Implement baseline DSP noise suppression for mic stream.
- [ ] Distinguish local voice stream from remote/system stream in stored transcript rows.
- [ ] Define quality benchmarks (SNR improvement, clipping/silence rates, transcript intelligibility).
- [ ] Add post-session quality scorecard per session.

### WS4: Service Reliability and Ops
- [ ] Harden single-instance service supervision and restart safety.
- [ ] Add structured logs and correlation IDs for meeting/session/source events.
- [ ] Add health dashboard focused on meeting-level readiness.
- [ ] Add alerting hooks for `degraded/down` during active sessions.

### WS5: UX and Workflow Simplification
- [ ] Replace advanced-first UI with "meeting-ready" defaults and one primary control surface.
- [ ] Keep advanced controls, but behind an explicit diagnostics panel.
- [ ] Add explicit "Capture Confidence" indicator and reasons.
- [ ] Add raw transcript export actions (copy/download) with zero summarization by default.

### WS6: Repo Health + Delivery
- [ ] Add CI gates for capture reliability tests and orchestration unit/integration tests.
- [ ] Add deterministic test fixtures for metadata and device-failure scenarios.
- [ ] Add migration plan/versioning if capture schema changes.
- [ ] Publish runbook: "What to do when system-audio disappears."

## Milestones

### M0: Decision + Baseline (Week 1)
- Final architecture decision record (hybrid native+service vs service-only).
- Current behavior baselined with reproducible measurements.

### M1: Deterministic Capture (Week 2)
- Source role model and device policy merged.
- No uncontrolled source churn during active sessions.

### M2: Metadata Orchestration (Week 3)
- Meeting metadata adapters + state machine merged.
- Auto start/stop working for Zoom/browser happy paths.

### M3: Quality Pass (Week 4)
- Noise suppression baseline merged.
- Mic/system separation visible and queryable in transcripts.

### M4: Production Readiness (Week 5)
- SLO instrumentation, alerts, runbooks, CI reliability gates complete.
- 14-day burn-in meets success metrics.

## Definition of Done
- Success metrics met for two consecutive weeks.
- Incident drills completed for at least 3 failure scenarios:
  1. system-audio route lost,
  2. metadata missing/late,
  3. service restart during active meeting.
- Exported raw transcript is consistently usable as downstream AI context.

## Suggested Child Issues
1. `ADR: choose hybrid native+service capture architecture`
2. `Source role model v2 and schema migration`
3. `Meeting metadata adapter: Zoom + browser`
4. `Session orchestrator state machine`
5. `Preflight capture checks and confidence scoring`
6. `Noise suppression baseline for mic channel`
7. `System-vs-local voice separation in transcript store`
8. `Single-instance supervisor hardening and watchdog`
9. `Capture reliability CI suite and fixtures`
10. `Raw transcript export UX (copy/download)`

## Suggested Labels
- `epic`
- `reliability`
- `audio`
- `capture`
- `ops`
- `enhancement`
