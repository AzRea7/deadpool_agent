"""Split runbooks into chunks: one per '## ' section (default) or fixed-size word windows."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_HEADER = re.compile(r"^#\s+(RB-\d{3})\s*:\s*(.+)$")
_PLATFORM = re.compile(r"^Platform:\s*(windows|linux|any)\s*$")
PLATFORMS = ("windows", "linux", "any")


@dataclass(frozen=True)
class Chunk:
    runbook_id: str
    title: str
    section: str
    text: str
    platform: str = "any"  # windows | linux | any

    @property
    def chunk_id(self) -> str:
        return f"{self.runbook_id}#{self.section}"


Chunker = Callable[[Path], list[Chunk]]


def _parse_header(path: Path) -> tuple[str, str, str, list[str]]:
    """Returns (runbook_id, title, platform, body lines). Platform line is optional (any)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    match = _HEADER.match(lines[0]) if lines else None
    if match is None:
        raise ValueError(f"{path.name}: first line must be '# RB-NNN: Title'")
    body = lines[1:]
    platform = "any"
    if body and (pm := _PLATFORM.match(body[0])):
        platform, body = pm.group(1), body[1:]
    elif body and body[0].startswith("Platform:"):
        raise ValueError(f"{path.name}: Platform must be one of {', '.join(PLATFORMS)}")
    return match.group(1), match.group(2).strip(), platform, body


def chunk_runbook(path: Path) -> list[Chunk]:
    runbook_id, title, platform, body_lines = _parse_header(path)

    chunks: list[Chunk] = []
    section: str | None = None
    body: list[str] = []
    for line in [*body_lines, "## END"]:
        if line.startswith("## "):
            text = "\n".join(body).strip()
            if section is not None and text:
                # Title and section go into the text so each chunk carries its context.
                chunk_text = f"{title} | {section}\n{text}"
                chunks.append(Chunk(runbook_id, title, section, chunk_text, platform))
            section, body = line[3:].strip(), []
        else:
            body.append(line)
    return chunks


def chunk_fixed(path: Path, size: int, overlap: int) -> list[Chunk]:
    """Slide a window of `size` words (stepping size - overlap) over the whole runbook body.

    Section headers stay in the text as plain words, so a window can span two sections.
    This is the comparison baseline for section chunking (ADR-002).
    """
    if not 0 <= overlap < size:
        raise ValueError("need 0 <= overlap < size")
    runbook_id, title, platform, body_lines = _parse_header(path)
    words = " ".join(body_lines).split()
    step = size - overlap
    chunks: list[Chunk] = []
    for start in range(0, max(len(words) - overlap, 1), step):
        window = " ".join(words[start : start + size])
        chunks.append(Chunk(runbook_id, title, f"w{start}", f"{title}\n{window}", platform))
    return chunks


def fixed_chunker(size: int, overlap: int) -> Chunker:
    return lambda path: chunk_fixed(path, size, overlap)


def load_runbooks(folder: Path, chunker: Chunker = chunk_runbook) -> list[Chunk]:
    return [c for p in sorted(folder.glob("RB-*.md")) for c in chunker(p)]
