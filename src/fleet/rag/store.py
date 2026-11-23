"""Load runbook chunks into Qdrant."""

import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from fleet.rag.chunk import Chunk
from fleet.rag.embed import DIM, embed_passages

COLLECTION = "runbooks"


def get_client(url: str = "http://localhost:6333") -> QdrantClient:
    return QdrantClient(url=url)


def index_chunks(client: QdrantClient, chunks: list[Chunk], collection: str = COLLECTION) -> int:
    """Rebuild the collection from scratch. Simple, and the runbook set is small."""
    if client.collection_exists(collection):
        client.delete_collection(collection)
    client.create_collection(
        collection, vectors_config=VectorParams(size=DIM, distance=Distance.COSINE)
    )
    vectors = embed_passages([c.text for c in chunks])
    points = [
        PointStruct(
            # Same chunk always gets the same point ID, so reruns don't duplicate.
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, c.chunk_id)),
            vector=v,
            payload={
                "runbook_id": c.runbook_id,
                "title": c.title,
                "section": c.section,
                "text": c.text,
            },
        )
        for c, v in zip(chunks, vectors, strict=True)
    ]
    client.upsert(collection, points=points)
    return len(points)
