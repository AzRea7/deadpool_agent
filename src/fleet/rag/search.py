"""search_runbook: the one function the agent will call."""

from dataclasses import dataclass

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchAny

from fleet.rag.embed import embed_query
from fleet.rag.store import COLLECTION, get_client


@dataclass(frozen=True)
class Hit:
    runbook_id: str
    title: str
    section: str
    score: float
    text: str
    platform: str = "any"


def search_runbook(
    question: str,
    k: int = 3,
    client: QdrantClient | None = None,
    collection: str = COLLECTION,
    fetch_per_hit: int = 10,
    platform: str | None = None,
) -> list[Hit]:
    """Return the top k distinct runbooks, each with its best-matching chunk.

    Several chunks of one runbook can all score high, so fetch k * fetch_per_hit results and
    keep only the first hit per runbook. Otherwise the top 3 could all be the same runbook.
    With a platform (windows or linux), only that platform's runbooks and the shared
    ('any') ones are searched, so a Linux disk never gets the IIS log runbook.
    """
    client = client or get_client()
    result = client.query_points(
        collection,
        query=embed_query(question),
        query_filter=_platform_filter(platform),
        limit=k * fetch_per_hit,
        with_payload=True,
    )
    hits: list[Hit] = []
    seen: set[str] = set()
    for point in result.points:
        payload = point.payload or {}
        runbook_id = str(payload["runbook_id"])
        if runbook_id in seen:
            continue
        seen.add(runbook_id)
        hits.append(
            Hit(
                runbook_id=runbook_id,
                title=str(payload["title"]),
                section=str(payload["section"]),
                score=float(point.score),
                text=str(payload["text"]),
                platform=str(payload.get("platform", "any")),
            )
        )
        if len(hits) == k:
            break
    return hits


def _platform_filter(platform: str | None) -> Filter | None:
    if platform is None:
        return None
    return Filter(must=[FieldCondition(key="platform", match=MatchAny(any=[platform, "any"]))])
