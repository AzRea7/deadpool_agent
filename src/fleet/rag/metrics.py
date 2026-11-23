"""Retrieval metrics for the runbook eval. Pure functions, no model or Qdrant needed."""

from collections.abc import Sequence
from dataclasses import dataclass


def rank_of(expected: str, ranked: Sequence[str]) -> int | None:
    """1-based rank of the expected runbook, or None if it was not returned."""
    for i, runbook_id in enumerate(ranked, start=1):
        if runbook_id == expected:
            return i
    return None


@dataclass(frozen=True)
class Scores:
    n: int
    k: int
    hit_at_1: float
    hit_at_k: float
    mrr_at_k: float


def score(cases: Sequence[tuple[str, Sequence[str]]], k: int) -> Scores:
    """Hit rate @1, hit rate @k and MRR@k over (expected, ranked runbook IDs) pairs.

    MRR@k adds 1/rank when the expected runbook is in the top k, else 0.
    """
    if not cases:
        raise ValueError("no cases")
    ranks = [rank_of(expected, ranked[:k]) for expected, ranked in cases]
    n = len(ranks)
    return Scores(
        n=n,
        k=k,
        hit_at_1=sum(r == 1 for r in ranks) / n,
        hit_at_k=sum(r is not None for r in ranks) / n,
        mrr_at_k=sum(1 / r for r in ranks if r is not None) / n,
    )
