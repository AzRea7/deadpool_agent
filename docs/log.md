# Build log

## 2026-11-09 · phase 0 (session 1)
- **Done:** uv repo + folder layout, docker compose (postgres/redis/qdrant healthy), ruff/mypy/pre-commit, smoke test, CI, `baseline/log_retention.ps1` draft (-WhatIf), README + ADR-001.
- **Blocked:** Azure CLI not installed locally, so `az account show`, `rg-fleet-health` and the $30 budget alert are still to do.
- **Next:** finish Azure groundwork, deploy the 2-instance test scale set with the retention task, fill a disk to prove the baseline works (due Nov 15).

## 2026-11-14 · phase 1 (started early; planned Nov 16-22)
- **Done:** runbooks RB-001 to RB-008 (4 sections each, 32 chunks), section chunker with tests, bge-small-en-v1.5 embeddings, Qdrant index (full rebuild), `search_runbook` returning the top 3 distinct runbook IDs, 10-question smoke eval. Smoke result, untuned: hit rate @3 = 1.00, top-1 = 0.70, MRR = 0.85. Top-1 misses: RB-002, RB-003 (503 question, RB-006 ranked first), RB-007 (RB-006 ranked first).
- **Caveat:** the smoke questions and runbooks were written by the same person, so the score is optimistic. Phase 2's 30 questions need wording that doesn't echo the runbooks.
- **Blocked:** Phase 0 Azure baseline still waits on an Azure account (moved to next week).
- **Next:** phase 2: 30-question eval set, hit rate and MRR script, compare section vs fixed-size chunking, tune top-k. Watch RB-006 outranking RB-003 and RB-007.

## 2026-11-23 · phase 2
- **Done:** 30-question eval set, `run_rag_eval.py` (hit@1/3/5, MRR, `--gate`), fixed-size chunker for comparison, ADR-002. Section chunks win: hit@3 = 1.00, MRR@3 = 0.95, top-1 = 0.90 (fixed-size top-1 is 0.77-0.83). Kept k = 3 and no further tuning, to avoid overfitting.
- **Blocked:** Phase 0 Azure baseline still not deployed (`az` installed, not logged in). Eval gate is not in CI yet because CI has no Qdrant or model.
- **Next:** finish the phase 0 baseline, then phase 3: chaos injector for disk and app pool, detector rules, sample store, incident schema in Postgres.

## 2026-11-24 · phase 0 (Azure) and phase 3
- **Done:** Phase 0 baseline written as Bicep (`baseline/bicep/`): 2-instance scale set, IIS logs on an 8 GB data disk, hourly retention task, automatic repairs, disk-free alert, $30 budget, plus `baseline/test_fill_disk.ps1`. Phase 3: detector rules (90/95% used, 12h projection from 6h slope, 3 failed probes in 5 min), Postgres sample store, incident table with one active incident per instance and scenario, local simulator, `chaos/fill_disk.ps1` and `chaos/stop_pool.ps1`, ADR-003. Simulated fill: one incident, opened 45 min in at about 64% used, touched 18 times, escalated to critical, 0 model calls. 27 tests; CI now runs mypy and the Postgres tests.
- **Blocked:** No Azure subscription yet. The Microsoft account's security info change holds security-code sign-ins until 2026-12-24, so the baseline isn't deployed and the chaos scripts haven't run on a real instance.
- **Next:** phase 4: read-only diagnose MCP server and LangGraph diagnose agent, built against recorded KQL and event-log fixtures until Azure is available.

## 2026-11-24 · phase 4 (offline part)
- **Done:** read-only diagnose MCP server (mcp 2.x `MCPServer`, six parameterized tools, fixture backend over 4 recorded incidents in `evals/incidents/`), LangGraph diagnose agent (prepare -> model <-> tools) with a Postgres checkpointer, Claude Opus 5.5 via the `anthropic` SDK, citation rule on `submit_diagnosis`, tool output wrapped as untrusted data. 41 tests pass offline with a scripted model, including resume from Postgres after a crash without repeating a model call.
- **Blocked:** live run with real Claude deferred: no Anthropic API key set yet, and it spends ~$1-2. Phase 4 isn't tagged until `evals/run_replay_eval.py` passes.
- **Next:** set the key, run the replay eval and the kill/resume demo, record numbers, tag `phase-4`. Then phase 5: action catalog, tiers, allowlist, dry-run hash, approval interrupt, audit log.

## 2026-11-24 · Linux/Apache support (ADR-006)
- **Done:** host type (`os`, `web_server`) on instances and incidents; scenario `app_pool_down` renamed `web_down`; runbooks tagged by platform, RB-009..RB-012 added for Linux/Apache, search filtered by platform; diagnose tools renamed OS-neutral (`system_log_tail`, `web_log_summary`, `web_server_state`) with journald/Apache sources on Linux; 4 Linux recorded incidents; `linux.bicep` + cloud-init, `log_retention.sh`, Linux fill test, `chaos/fill_disk.sh`, `chaos/stop_apache.sh`. Retrieval on 42 questions: hit@3 1.00, MRR@3 0.92 (Windows 0.93, Linux 0.90). 50 tests.
- **Blocked:** same as before: no Azure until 2026-12-24, no Anthropic key for the live replay eval (now 8 cases).
- **Next:** phase 5 action catalog, built per-OS from the start.
