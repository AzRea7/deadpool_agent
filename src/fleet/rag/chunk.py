"""Split runbooks into one chunk per '## ' section."""

import re
from dataclasses import dataclass
from pathlib import Path

_HEADER = re.compile(r"^#\s+(RB-\d{3})\s*:\s*(.+)$")


@dataclass(frozen=True)
class Chunk:
    runbook_id: str
    title: str
    section: str
    text: str

    @property
    def chunk_id(self) -> str:
        return f"{self.runbook_id}#{self.section}"


def chunk_runbook(path: Path) -> list[Chunk]:
    lines = path.read_text(encoding="utf-8").splitlines()
    match = _HEADER.match(lines[0]) if lines else None
    if match is None:
        raise ValueError(f"{path.name}: first line must be '# RB-NNN: Title'")
    runbook_id, title = match.group(1), match.group(2).strip()

    chunks: list[Chunk] = []
    section: str | None = None
    body: list[str] = []
    for line in [*lines[1:], "## END"]:
        if line.startswith("## "):
            text = "\n".join(body).strip()
            if section is not None and text:
                # Title and section go into the text so each chunk carries its context.
                chunks.append(Chunk(runbook_id, title, section, f"{title} | {section}\n{text}"))
            section, body = line[3:].strip(), []
        else:
            body.append(line)
    return chunks


def load_runbooks(folder: Path) -> list[Chunk]:
    return [c for p in sorted(folder.glob("RB-*.md")) for c in chunk_runbook(p)]
