import pytest

from fleet.rag.metrics import rank_of, score


def test_rank_of() -> None:
    assert rank_of("RB-002", ["RB-001", "RB-002"]) == 2
    assert rank_of("RB-009", ["RB-001"]) is None


def test_score_hit_rate_and_mrr() -> None:
    cases = [
        ("RB-001", ["RB-001", "RB-002", "RB-003"]),  # rank 1
        ("RB-002", ["RB-001", "RB-002", "RB-003"]),  # rank 2
        ("RB-003", ["RB-001", "RB-002", "RB-004"]),  # miss
        ("RB-004", ["RB-001", "RB-002", "RB-003", "RB-004"]),  # rank 4, outside k=3
    ]
    s = score(cases, k=3)
    assert s.hit_at_1 == pytest.approx(0.25)
    assert s.hit_at_k == pytest.approx(0.5)
    assert s.mrr_at_k == pytest.approx((1 + 0.5) / 4)


def test_score_rejects_empty() -> None:
    with pytest.raises(ValueError):
        score([], k=3)
