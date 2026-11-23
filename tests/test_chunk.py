from pathlib import Path

import pytest

from fleet.rag.chunk import chunk_fixed, chunk_runbook


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


def test_fixed_windows_overlap(tmp_path: Path) -> None:
    p = tmp_path / "RB-002.md"
    words = " ".join(f"w{i}" for i in range(10))
    p.write_text(f"# RB-002: Unknown\n\n## Symptoms\n{words}\n")
    chunks = chunk_fixed(p, size=6, overlap=2)
    # Body is "## Symptoms w0 ... w9" = 12 words; windows start at 0, 4, 8.
    assert [c.section for c in chunks] == ["w0", "w4", "w8"]
    assert chunks[1].text.endswith("w2 w3 w4 w5 w6 w7")
    assert all(c.text.startswith("Unknown\n") for c in chunks)


def test_fixed_rejects_bad_overlap(tmp_path: Path) -> None:
    p = tmp_path / "RB-001.md"
    p.write_text("# RB-001: Disk\n## Symptoms\nx\n")
    with pytest.raises(ValueError):
        chunk_fixed(p, size=4, overlap=4)
