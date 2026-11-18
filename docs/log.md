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
