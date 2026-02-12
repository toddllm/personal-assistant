# EPIC: Productionize Personal Assistant Capture Stack

## Summary
Move `personal-assistant` from local prototype to production-grade service with reliable capture, operational observability, controlled releases, and repository quality gates.

## Why
Capture lapses are unacceptable for core assistant behavior. We need deterministic uptime, clear readiness signals, and an auditable path from code change to safe deployment.

## Success Metrics
- No missed transcript windows during scheduled capture windows for 14 consecutive days.
- `v1/capture/readiness` reports `ready` for >99% of sampled checks during active hours.
- Mean time to detect capture degradation < 1 minute.
- Mean time to recover from source failure < 3 minutes.
- CI health gate (lint + tests) required on every PR.

## Scope
- `audio-assist` service reliability and observability.
- Service operations and runbooks.
- Repo health enforcement (tests/lint/automation).
- Release hygiene and rollback procedures.

## Out of Scope
- Full product redesign of UI.
- Replacing current ASR engine in this epic.
- Multi-region/cloud deployment.

## Workstreams

### WS1: Runtime Reliability
- [ ] Harden source lifecycle and restart behavior.
- [ ] Add capture watchdog + readiness thresholds tuned for live use.
- [ ] Add controlled startup/shutdown service wrappers and pid management.
- [ ] Add failure injection tests for source interruption.

### WS2: Logging and Observability
- [ ] Structured logs with rotation and persistent files.
- [ ] Correlate events by source/session ids.
- [ ] Add readiness + transcriber dashboards (console/events UI).
- [ ] Add alert hooks for `down`/`degraded` states.

### WS3: Capture Quality Assurance
- [ ] Build preflight checklist (mic active + transcript write confirmation).
- [ ] Validate overlap with event windows under real meeting conditions.
- [ ] Define pass/fail quality metrics per session.
- [ ] Implement automated smoke capture test command.

### WS4: Repo Health and CI
- [ ] Add/expand tests for capture readiness and source management.
- [ ] Add CI workflow for lint + tests.
- [ ] Add local repo-health script used pre-commit.
- [ ] Publish contribution standards and commit gate.

### WS5: Release and Operations
- [ ] Define release checklist and rollback steps.
- [ ] Add runbook for incident handling and root-cause template.
- [ ] Add environment configuration matrix (dev/stage/prod).
- [ ] Track SLOs and weekly reliability review.

## Milestones

### M1: Service Baseline (Week 1)
- Logging, readiness endpoint, service script, health script merged.
- Initial tests in place and passing locally.

### M2: Operational Hardening (Week 2)
- Failure-mode tests, alert hooks, and runbooks published.
- Degraded/down detection validated with chaos tests.

### M3: Production Readiness (Week 3)
- CI mandatory for merge.
- 7-day reliability burn-in complete with no critical gaps.

## Definition of Done
- All workstream checkboxes complete.
- CI green on main.
- Runbook validated during at least one simulated incident.
- Reliability metrics meet thresholds for two consecutive weeks.

## Suggested GitHub Child Issues
1. `Production logging + rotation for audio-assist`
2. `Service supervisor script + pid + readiness commands`
3. `Capture readiness tests and failure-mode coverage`
4. `CI workflow: lint + tests required`
5. `Ops runbook: incident response and rollback`
6. `Reliability burn-in tracking and SLO review`

## Suggested Labels
- `epic`
- `production`
- `reliability`
- `observability`
- `repo-health`

