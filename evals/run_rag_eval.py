"""Runbook retrieval eval: hit rate and MRR, compared across chunking strategies.

    uv run python evals/run_rag_eval.py                 # all strategies, prints a table
    uv run python evals/run_rag_eval.py --gate section  # exit 1 if 'section' misses the targets

Needs Qdrant running (infra/docker-compose.yml). Each strategy gets its own eval_* collection,
so the production 'runbooks' collection is never touched.
"""

import argparse
import json
import sys
from pathlib import Path

from fleet.rag.chunk import Chunker, chunk_runbook, fixed_chunker, load_runbooks
from fleet.rag.metrics import Scores, rank_of, score
from fleet.rag.search import search_runbook
from fleet.rag.store import get_client, index_chunks

HIT_RATE_TARGET = 0.85  # hit rate @3
MRR_TARGET = 0.70  # MRR @3
DEPTH = 5  # how many distinct runbooks to retrieve per question

STRATEGIES: dict[str, Chunker] = {
    "section": chunk_runbook,
    "fixed-64w": fixed_chunker(64, 16),
    "fixed-128w": fixed_chunker(128, 32),
    "fixed-256w": fixed_chunker(256, 64),
    "whole-doc": fixed_chunker(10_000, 0),
}


def load_cases(path: Path) -> list[dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def run(name: str, chunker: Chunker, cases: list[dict[str, str]]) -> dict[str, object]:
    client = get_client()
    collection = f"eval_{name.replace('-', '_')}"
    chunks = load_runbooks(Path("runbooks"), chunker)
    index_chunks(client, chunks, collection)
    # Search the way the agent does: filtered to the host's platform plus shared runbooks.
    ranked = [
        (
            c["expected"],
            [
                h.runbook_id
                for h in search_runbook(
                    c["q"], DEPTH, client, collection, platform=c.get("platform")
                )
            ],
        )
        for c in cases
    ]
    by_k: dict[int, Scores] = {k: score(ranked, k) for k in (1, 3, 5)}
    misses = [
        {"q": c["q"], "expected": exp, "rank": rank_of(exp, ids), "got": ids[:3]}
        for c, (exp, ids) in zip(cases, ranked, strict=True)
        if ids[:1] != [exp]
    ]
    per_platform = {
        plat: score([r for c, r in zip(cases, ranked, strict=True) if c.get("platform") == plat], 3)
        for plat in sorted({c.get("platform", "any") for c in cases})
    }
    return {
        "per_platform": {
            p: {"n": s.n, "hit@1": s.hit_at_1, "hit@3": s.hit_at_k, "mrr@3": s.mrr_at_k}
            for p, s in per_platform.items()
        },
        "strategy": name,
        "chunks": len(chunks),
        "hit@1": by_k[1].hit_at_1,
        "hit@3": by_k[3].hit_at_k,
        "hit@5": by_k[5].hit_at_k,
        "mrr@3": by_k[3].mrr_at_k,
        "mrr@5": by_k[5].mrr_at_k,
        "top1_misses": misses,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", type=Path, default=Path("evals/questions.jsonl"))
    parser.add_argument("--strategies", nargs="*", default=list(STRATEGIES))
    parser.add_argument("--gate", help="strategy that must meet the targets (exit 1 if not)")
    parser.add_argument("--out", type=Path, default=Path("evals/results/rag_eval.json"))
    args = parser.parse_args()

    cases = load_cases(args.questions)
    results = [run(name, STRATEGIES[name], cases) for name in args.strategies]

    print(f"\n{len(cases)} questions, targets: hit@3 >= {HIT_RATE_TARGET}, MRR@3 >= {MRR_TARGET}\n")
    print(f"{'strategy':<12}{'chunks':>7}{'hit@1':>7}{'hit@3':>7}{'hit@5':>7}{'MRR@3':>7}")
    for r in results:
        print(
            f"{r['strategy']:<12}{r['chunks']:>7}{r['hit@1']:>7.2f}{r['hit@3']:>7.2f}"
            f"{r['hit@5']:>7.2f}{r['mrr@3']:>7.2f}"
        )
    for r in results:
        per = r["per_platform"]
        assert isinstance(per, dict)
        parts = [
            f"{p} n={v['n']} hit@1={v['hit@1']:.2f} MRR@3={v['mrr@3']:.2f}" for p, v in per.items()
        ]
        print(f"  {r['strategy']:<12}" + "   ".join(parts))
    for r in results:
        misses = r["top1_misses"]
        assert isinstance(misses, list)
        print(f"\n{r['strategy']} top-1 misses ({len(misses)}):")
        for m in misses:
            print(f"  {m['expected']} rank={m['rank']} got={m['got']} | {m['q']}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {args.out}")

    if args.gate:
        gated = next(r for r in results if r["strategy"] == args.gate)
        hit3, mrr3 = gated["hit@3"], gated["mrr@3"]
        assert isinstance(hit3, float) and isinstance(mrr3, float)
        if hit3 < HIT_RATE_TARGET or mrr3 < MRR_TARGET:
            print(f"GATE FAILED: {args.gate} hit@3={hit3:.2f} MRR@3={mrr3:.2f}")
            return 1
        print(f"GATE PASSED: {args.gate} hit@3={hit3:.2f} MRR@3={mrr3:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
