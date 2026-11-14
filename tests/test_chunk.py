from pathlib import Path

import pytest

from fleet.rag.chunk import chunk_runbook


def test_one_chunk_per_section(tmp_path: Path) -> None:
    p = tmp_path / "RB-001.md"
    p.write_text("# RB-001: Disk\n\n## Symptoms\nfull\n\n## Diagnosis\ncheck logs\n")
    chunks = chunk_runbook(p)
    assert [c.section for c in chunks] == ["Symptoms", "Diagnosis"]
    assert all(c.runbook_id == "RB-001" for c in chunks)
    assert chunks[0].chunk_id == "RB-001#Symptoms"


def test_bad_header_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "RB-x.md"
    p.write_text("no header\n## Symptoms\nx\n")
    with pytest.raises(ValueError):
        chunk_runbook(p)
