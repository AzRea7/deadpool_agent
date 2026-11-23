# Fleet Health Agent

> Deterministic code detects and executes; the model handles the ambiguous middle, diagnosis and runbook choice; every action is tiered, audited and verified.

A self-healing health agent for Azure Windows/IIS VM scale sets: it detects trouble early, diagnoses it, fixes what is safe, and proves the fix worked.

## Quickstart

```bash
uv sync
docker compose -f infra/docker-compose.yml up -d   # postgres, redis, qdrant
uv run pytest
uv run python -m fleet.rag.index                   # embed runbooks/ into Qdrant
uv run python evals/run_smoke.py                   # 10-question retrieval smoke eval
uv run python evals/run_rag_eval.py                # 30-question eval, compares chunking strategies
```

## Eval results

| Eval | Metric | Target | Result |
|------|--------|--------|--------|
| Runbook RAG smoke (10 questions, phase 1, untuned) | Hit rate @3 / top-1 / MRR | — | 1.00 / 0.70 / 0.85 |
| Runbook RAG (30 questions, phase 2, section chunks) | Hit rate @3 | >= 0.85 | 1.00 (top-1: 0.90) |
| Runbook RAG (30 questions, phase 2, section chunks) | MRR @3 | >= 0.70 | 0.95 |
| Replay incidents | Pass rate | — | — |

## Docs

- [ADR-001: Baseline first](docs/adr/ADR-001-baseline-first.md)
- [ADR-002: Chunking strategy](docs/adr/ADR-002-chunking-strategy.md)
- [Build log](docs/log.md)
