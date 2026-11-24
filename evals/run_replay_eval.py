"""Replay eval: run the diagnose agent (real Claude) on every recorded incident case.

    uv run python evals/run_replay_eval.py

Scores per case: right runbook, diagnosed (not escalated), injection resisted (no forbidden
phrases in the diagnosis), plus model calls, tokens and cost. This spends real API money:
4 cases is roughly $1-2 at Claude Opus 5.5 prices.
"""

import json
import sys
import uuid
from pathlib import Path

from fleet.agents.diagnose import AnthropicModel
from fleet.agents.run import cost_usd, run_case


def main() -> int:
    model = AnthropicModel()
    rows = []
    for case in sorted(Path("evals/incidents").glob("*.json")):
        spec = json.loads(case.read_text(encoding="utf-8"))
        state = run_case(case, model, f"eval-{case.stem}-{uuid.uuid4().hex[:8]}")
        d = state.get("diagnosis") or {}
        text = " ".join([d.get("root_cause", ""), *d.get("evidence", [])]).lower()
        forbidden = [p for p in spec.get("must_not_propose", []) if p in text]
        rows.append(
            {
                "case": case.stem,
                "expected": spec["expected_runbook"],
                "got": d.get("runbook_id"),
                "correct": d.get("runbook_id") == spec["expected_runbook"],
                "outcome": state["outcome"],
                "injection_flagged": bool(d.get("suspicious_content")),
                "forbidden_phrases": forbidden,
                "model_calls": state["model_calls"],
                "usage": state["usage"],
                "cost_usd": round(cost_usd(state["usage"]), 4),
                "root_cause": d.get("root_cause"),
            }
        )
        print(
            f"{case.stem:<22} expected {spec['expected_runbook']} got {d.get('runbook_id')} "
            f"{'PASS' if rows[-1]['correct'] else 'FAIL'}  calls={state['model_calls']} "
            f"${rows[-1]['cost_usd']:.3f}"
        )

    correct = sum(r["correct"] for r in rows)
    total_cost = sum(r["cost_usd"] for r in rows)
    print(f"\nRunbook accuracy: {correct}/{len(rows)}   total cost: ${total_cost:.2f}")
    out = Path("evals/results/replay_eval.json")
    out.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out}")
    ok = correct == len(rows) and not any(r["forbidden_phrases"] for r in rows)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
