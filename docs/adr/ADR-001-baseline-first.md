# ADR-001: Baseline first

Status: Accepted · Phase 0 · 2026-11-09

## Context

Plain IIS-log cleanup does not need an LLM. Disk-full incidents on Windows/IIS scale sets are mostly caused by IIS logs that nobody rotates, and that problem has well-known, boring fixes. Putting an agent in front of a problem a scheduled task already solves adds cost, latency and risk without adding value.

## Decision

Phase 0 ships the boring fixes in a test scale set before any agent code:

- a scheduled log-retention task (`baseline/log_retention.ps1`),
- IIS logs on a data disk or shipped to Log Analytics,
- automatic instance repairs,
- a disk-free alert.

The agent then covers only what the baseline cannot: unknown disk consumers, hung app pools, memory leaks and error spikes.

## Alternatives

- **Agent handles everything, including log cleanup.** Rejected: an LLM in the loop for a deterministic, recurring task is slower, costs tokens and widens the blast radius of a bad decision.
- **Baseline only, no agent.** Rejected: it cannot diagnose novel disk consumers, hung pools, leaks or 5xx spikes, which is where time-to-recover actually goes.

## Consequences

- The agent's evals measure the hard cases, not problems a cron job solves.
- Phase 0 must prove the baseline alone keeps a filled test disk under threshold.
- Better interview story: knowing when *not* to use an agent.
- Open question: if IIS logs must be retained, archive to Blob Storage before deleting.
