# ADR-005: Verification loop

Status: Accepted · Phase 5 · 2026-11-24

## Context

An action that ran is not an action that worked. A recycle can succeed and the pool crash again a minute later; deleting old logs can free too little; a reload can leave the real cause in place. Without a check, the system reports success, the detector reopens the incident, and the agent loops.

## Decision

After every action, the graph's verify step waits, then re-reads the metric that opened the incident through the same executor, and decides:

| Scenario | Metric | Recovered when |
|---|---|---|
| `disk_filling` | system volume used % (C: or /) | below 90% and lower than before the action |
| `web_down` | health probe | passing |

- **Recovered:** outcome `resolved`; the incident can close.
- **Not recovered:** outcome `escalated`, with the reason and the evidence: the plan, the dry run, the before and after values. No action is ever marked successful without this check, and the verification is written to the audit log like the action itself.
- The wait is a parameter: 120 seconds by default (5 seconds in the local runner, 0 in tests). The runbooks state the intended wait per action (2 minutes for a recycle or restart, 5 for a memory fix).
- Escalation does not retry. The Tier 1 hourly limit and the 60-minute cooldown make a second automatic attempt impossible unless the incident turns critical, so a fix that doesn't hold goes to a human instead of looping.

## Alternatives

- **Trust the action's exit code.** Rejected: "recycle succeeded" says nothing about whether the site serves traffic.
- **Let the model judge recovery.** Rejected: recovery is a number against a threshold, and that is deterministic code's job (ADR-003).
- **Retry automatically on failure.** Rejected: if the first fix didn't hold, the diagnosis or the fix was wrong, and repeating it hides that.

## Consequences

- Tests cover both branches: a log cleanup that recovers the disk, and a recycle on a pool whose bug persists (`recurs_after_fix`) that escalates instead of reporting success.
- The verification reads one instance's metric shortly after the action. Slower relapses (memory that climbs back over an hour) are caught by the detector opening a new incident, which the cooldown then routes to a human.
- Phase 6 replaces the simulated read with Log Analytics, where the wait matters more: samples arrive every minute and the probe needs 3 passes in a row.
