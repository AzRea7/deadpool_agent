# Build log

## 2026-11-09 · phase 0 (session 1)
- **Done:** uv repo + folder layout, docker compose (postgres/redis/qdrant healthy), ruff/mypy/pre-commit, smoke test, CI, `baseline/log_retention.ps1` draft (-WhatIf), README + ADR-001.
- **Blocked:** Azure CLI not installed locally, so `az account show`, `rg-fleet-health` and the $30 budget alert are still to do.
- **Next:** finish Azure groundwork, deploy the 2-instance test scale set with the retention task, fill a disk to prove the baseline works (due Nov 15).
