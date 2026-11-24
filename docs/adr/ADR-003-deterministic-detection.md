# ADR-003: Deterministic detection, model for diagnosis

Status: Accepted · Phase 3 · 2026-11-24

## Context

Something has to watch every instance every 15 minutes and decide whether anything is wrong. Most of the time nothing is. The question is whether that watcher should be a model or plain code.

## Decision

The detector is plain code with no LLM (`src/fleet/detector/`):

- **Rules** (`rules.py`) are pure functions over samples, with the starting values from the scenarios table: C: at 90% (warning) or 95% (critical) used, or projected full within 12h from a least-squares slope over 6h (at least 6 points). For app pools, 3 failed health probes within 5 minutes. Data older than 30 minutes is ignored; a blind detector is RB-008's problem, not a disk alert.
- **Samples** go into Postgres (`samples` table), so the slope is computed from stored history and not only the latest fetch.
- **Incidents**: at most one active incident per instance and scenario. A partial unique index (`WHERE closed_at IS NULL`) plus `INSERT ... ON CONFLICT DO UPDATE` makes this race-safe. Repeats bump `occurrences` and `last_seen_at`, and severity only goes up. Escalated incidents stay active, so the detector never reopens what a human owns.
- **Sources** sit behind a `MetricsSource` interface. `FakeSource` drives local runs and tests; the Azure Monitor source plugs in once the baseline is deployed.

The model only starts for an open incident, to diagnose it and choose a runbook (phase 4).

## Alternatives

- **An LLM reads metrics on every cycle.** Rejected: it costs tokens on a healthy fleet, it is non-deterministic and hard to unit-test, and it adds a prompt-injection surface (log text) to the always-on path.
- **Azure Monitor alert rules only.** Kept as the baseline (ADR-001), but alert rules cannot de-duplicate into an incident lifecycle, hold state for the agent, or be replayed in tests.
- **Fixed thresholds without a slope.** Rejected: they fire too late. In the simulation, the slope rule opens the incident 45 minutes into a fill at about 64% used, more than 3 hours before the 90% line.

## Consequences

- A healthy fleet costs zero model calls. Every cycle logs `model_calls=0`, which is the demo line.
- The rules are unit-tested (`tests/test_rules.py`) and the de-duplication is tested against real Postgres in CI (`tests/test_detector_db.py`).
- Thresholds are starting values. Tune them on real baseline data once the scale set is deployed, especially the slope window and minimum points, which trade early warning against noise.
- Not done yet: hysteresis after an action (60 minutes, unless 95% disk or the pool fails again) and single-flight per instance arrive with the action layer in phases 5-6.
