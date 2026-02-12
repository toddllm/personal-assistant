# ADR-0001: Monorepo Service Structure (No Git Submodules for First-Party Services)

## Status
Accepted

## Date
2026-02-11

## Context
This repository hosts multiple tightly-coupled services:
- `audio-assist` (capture, transcript UI, readiness)
- `google-sync-service` (email/calendar sync)
- `speaker-service` (optional diarization)
- `tts-service` (speech synthesis)

Question: should each service be split into separate repositories and linked via git submodules?

## Decision
Use a single monorepo for first-party services. Do **not** use git submodules for these services.

## Rationale
1. Shared release cadence: these services frequently evolve together.
2. Operational coupling: capture/readiness and sync/tts integrations are tested together.
3. Lower maintenance overhead: submodules add sync friction and failure modes.
4. Better CI ergonomics: one repo-health gate across all service changes.

Submodules remain acceptable only for:
- external third-party codebases intentionally vendored in read-only mode, or
- truly independent products with separate ownership/release cycles.

## Consequences
### Positive
- Simpler developer workflow.
- Single source of truth for docs/runbooks.
- Easier cross-service refactors and coordinated commits.

### Negative
- Larger repo over time.
- Requires disciplined boundaries and directory conventions.

## Follow-up
- Keep service-specific docs under `docs/services/`.
- Keep shared standards in root docs (`README.md`, `docs/repo-health.md`).
- Enforce health checks via CI (`.github/workflows/repo-health.yml`).
