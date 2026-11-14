"""Turn text into 384-number vectors with bge-small."""

from functools import lru_cache

from sentence_transformers import SentenceTransformer

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIM = 384
# bge models search better when the question (not the passage) carries this prefix.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


@lru_cache(maxsize=1)
def _model() -> SentenceTransformer:
    return SentenceTransformer(MODEL_NAME)


def embed_passages(texts: list[str]) -> list[list[float]]:
    vectors = _model().encode(texts, normalize_embeddings=True)
    return [[float(x) for x in v] for v in vectors]


def embed_query(question: str) -> list[float]:
    vector = _model().encode(QUERY_PREFIX + question, normalize_embeddings=True)
    return [float(x) for x in vector]
