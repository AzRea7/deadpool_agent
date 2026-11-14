"""10-question smoke eval: uv run python evals/run_smoke.py (Qdrant running, index built)."""

import json
from pathlib import Path

from fleet.rag.search import search_runbook

lines = Path("evals/smoke_questions.jsonl").read_text(encoding="utf-8").splitlines()
cases = [json.loads(line) for line in lines if line.strip()]

found = 0
for case in cases:
    ids = [hit.runbook_id for hit in search_runbook(case["q"], k=3)]
    ok = case["expected"] in ids
    found += ok
    print(f"{'PASS' if ok else 'MISS'}  {case['expected']}  got {ids}  | {case['q']}")

print(f"\nHit rate @3: {found}/{len(cases)} = {found / len(cases):.2f}")
