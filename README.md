# Fleet Health Agent

> Deterministic code detects and executes; the model handles the ambiguous middle, diagnosis and runbook choice; every action is tiered, audited and verified.

A self-healing health agent for Azure VM scale sets running **Windows/IIS or Linux/Apache**: it detects trouble early, diagnoses it, fixes what is safe, and proves the fix worked.

## Quickstart

```bash
uv sync
docker compose -f infra/docker-compose.yml up -d   # postgres, redis, qdrant
uv run pytest
uv run python -m fleet.rag.index                   # embed runbooks/ into Qdrant
uv run python evals/run_smoke.py                   # 10-question retrieval smoke eval
uv run python evals/run_rag_eval.py                # 42-question eval (Windows + Linux), compares chunking
uv run python -m fleet.detector.job --sim fill     # detector on a simulated disk fill (no LLM)
uv run python -m fleet.detector.job --sim fill-linux   # same on a Linux/Apache host
uv run python -m fleet.agents.run --case evals/incidents/pool_crash.json   # diagnose agent (needs ANTHROPIC_API_KEY)
uv run python evals/run_replay_eval.py             # all recorded incidents, real model (~$1-2)
uv run python -m fleet.agents.run --case evals/incidents/disk_freb.json --remediate   # diagnose, plan, gate, act, verify
uv run python -m fleet.agents.run --case ... --thread T --approve <you> --plan-hash H  # approve a paused Tier 2 plan
```

## Eval results

| Eval | Metric | Target | Result |
|------|--------|--------|--------|
| Runbook RAG smoke (10 questions, phase 1, untuned) | Hit rate @3 / top-1 / MRR | — | 1.00 / 0.70 / 0.85 |
| Runbook RAG (30 Windows questions, phase 2, section chunks) | Hit rate @3 / MRR @3 | >= 0.85 / >= 0.70 | 1.00 / 0.95 |
| Runbook RAG (42 questions, Windows + Linux, platform-filtered) | Hit rate @3 / MRR @3 / top-1 | >= 0.85 / >= 0.70 | 1.00 / 0.92 / 0.86 |
| Governance + incident graph (phase 5, scripted model) | Unapproved Tier 2 executions / unaudited or unverified actions | 0 / 0 | 0 / 0 (99 tests) |
| Replay incidents (8 recorded cases, 4 per OS, phase 4) | Right runbook / injection resisted | 8/8 | not run yet |

## Docs

- [ADR-001: Baseline first](docs/adr/ADR-001-baseline-first.md)
- [ADR-002: Chunking strategy](docs/adr/ADR-002-chunking-strategy.md)
- [ADR-003: Deterministic detection](docs/adr/ADR-003-deterministic-detection.md)
- [ADR-004: Action catalog and tiers](docs/adr/ADR-004-action-catalog-and-tiers.md)
- [ADR-005: Verification loop](docs/adr/ADR-005-verification-loop.md)
- [ADR-006: Windows/IIS and Linux/Apache hosts](docs/adr/ADR-006-linux-apache.md)
- [Build log](docs/log.md)
