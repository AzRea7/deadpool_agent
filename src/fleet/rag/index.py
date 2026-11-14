"""Rebuild the runbook index: uv run python -m fleet.rag.index"""

from pathlib import Path

from fleet.rag.chunk import load_runbooks
from fleet.rag.store import get_client, index_chunks


def main() -> None:
    chunks = load_runbooks(Path("runbooks"))
    runbooks = len({c.runbook_id for c in chunks})
    print(f"Indexed {index_chunks(get_client(), chunks)} chunks from {runbooks} runbooks")


if __name__ == "__main__":
    main()
