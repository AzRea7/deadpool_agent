# ADR-004: Action catalog and tiers

Status: Accepted · Phase 5 · 2026-11-24

## Context

The agent has to change things on production instances: delete logs, recycle an app pool, reload Apache, sometimes reimage or roll back. A model that writes its own scripts can delete the wrong folder, act on the wrong instance, or follow an instruction planted in a log line. The risk has to sit in code we can test, not in a prompt.

## Decision

A fixed catalog of parameterized actions (`src/fleet/governance/catalog.py`), 15 in total, each with a risk tier, the platforms it runs on, the runbooks that allow it, a Pydantic parameter schema and a fixed script template. The model picks an action name and parameters; it never writes PowerShell, shell or az commands, and it cannot choose the instance (always the incident's own).

| Tier | Runs | Examples |
|---|---|---|
| 0 | Automatically, pre-approved by policy | `delete_iis_logs_older_than`, `clear_temp`, `delete_apache_logs_older_than`, `journal_vacuum` |
| 1 | Automatically, one instance, at most once per instance per hour | `recycle_app_pool`, `restart_iis_services`, `graceful_reload_service`, `restart_service` |
| 2 | After a named human approves the exact plan hash | `delete_paths`, `reimage_instance`, `scale_out`, `rollback_deploy` |
| Never | Not in the catalog; refused by the allowlist even inside `delete_paths` | wwwroot, `/var/www`, uploads, databases, user profiles, `..`, system folders |

Every action passes the gates in `ActionService` (`src/fleet/governance/service.py`), in order:

1. **Catalog:** the action exists, runs on this platform, and the parameters validate (no extra fields; names match a strict pattern, so a value can't carry a command).
2. **Allowlist:** pool and service names are listed; Tier 0 deletes stay under fixed roots; never-delete rules hold for every target, re-checked inside the dry run.
3. **Citation:** the plan cites the diagnosis runbook, which `search_runbook` returned, and that runbook allows this action.
4. **Dry run:** a fresh dry run, computed at execution time, must match the plan hash (instance, action, parameters and exact targets), be under 15 minutes old, and find something to change.
5. **Blast radius:** global kill switch, instance not upgrading or scaling in, one instance per plan, the Tier 1 hourly limit, and a 60-minute cooldown after any action unless the incident is critical.
6. **Approval (Tier 2):** a LangGraph interrupt; the run resumes only with an approver ID that isn't a system actor, the exact plan hash and an "approve" decision.

Every block, approval, execution and verification writes a row to an append-only audit log; Postgres rejects UPDATE and DELETE on it with a trigger.

**Separation of duties.** The diagnose agent holds only read-only diagnose tools. The planner holds `list_actions`, `dry_run_action` and `submit_plan`. Nobody holding a model holds execute: the act step is plain code, and the action MCP server (`src/fleet/mcp_servers/action_server.py`), which owns the action credentials, re-runs every gate inside `execute_action`.

## Alternatives

- **Free-form scripts from the model, reviewed by a second model.** Rejected: the reviewer shares the failure modes, and nothing is testable.
- **Approve every action by hand.** Rejected: old-log cleanup at 3 a.m. should not wait for a person, and approval fatigue makes the important approvals worse.
- **Approve an action name rather than a hash.** Rejected: the host can change while someone reads the request. Binding the approval to the dry-run hash means the approved file list is exactly the one deleted, or nothing runs.

## Consequences

- Adding an action is a code change with tests: a schema, a template, a tier, the runbooks that allow it, and the runbook text naming it. A test checks that the runbooks and the catalog agree both ways.
- The model's job shrinks to choosing; when nothing fits, it hands humans a proposal (outcome `proposed`).
- Crash safety falls out of the hash: if the process dies after an action but before its checkpoint, the replayed act step finds a changed host, the hash no longer matches, and the action does not run twice.
- Tested offline against simulated hosts built from the recorded incidents (31 governance tests, 15 graph tests). The Azure Run Command executor implements the same interface in phase 6.
