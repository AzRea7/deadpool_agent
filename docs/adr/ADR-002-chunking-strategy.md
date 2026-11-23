# ADR-002: Chunking strategy

Status: Accepted · Phase 2 · 2026-11-23

## Context

`search_runbook` must return the right runbook ID so the diagnosis agent can cite it. Runbooks are short (8 files, about 150-450 words each) and every one has the same four sections: Symptoms, Diagnosis, Allowed actions, Rollback. The question is how to cut them into chunks for bge-small-en-v1.5 embeddings in Qdrant.

Eval: 30 questions in `evals/questions.jsonl` (3-4 per runbook), written as alerts and tickets rather than copied from runbook wording, including near-misses (single-instance 503 vs whole-host outage, leak vs crash, unknown disk consumer vs IIS logs). The script is `evals/run_rag_eval.py`. Each strategy is indexed into its own collection and scored on distinct runbook IDs.

| Strategy | Chunks | Hit@1 | Hit@3 | Hit@5 | MRR@3 |
|---|---|---|---|---|---|
| **Section (one chunk per `##`, title prefixed)** | 32 | **0.90** | **1.00** | 1.00 | **0.95** |
| Fixed 64 words, 16 overlap | 46 | 0.77 | 1.00 | 1.00 | 0.87 |
| Fixed 128 words, 32 overlap | 23 | 0.83 | 1.00 | 1.00 | 0.92 |
| Fixed 256 words, 64 overlap | 12 | 0.83 | 1.00 | 1.00 | 0.91 |
| Whole runbook | 8 | 0.83 | 1.00 | 1.00 | 0.91 |

Targets: hit rate @3 >= 0.85, MRR @3 >= 0.70.

## Decision

Keep section chunking with the runbook title prefixed to every chunk. Return the top **k = 3** distinct runbooks, fetching `k * 10` raw hits so that several strong chunks from one runbook cannot push others out. No further tuning: section chunking already passes both targets with margin, and tuning more against 30 questions would overfit them.

## Alternatives

- **Fixed-size windows.** Worse on every size tried. Small windows (64 words) split a symptom list from its runbook context and confuse the two disk runbooks (RB-001 vs RB-002). Large windows behave like the whole document.
- **Whole runbook as one chunk.** Simplest, but it blurs neighbours that share vocabulary (RB-003, RB-005, RB-006 all mention 503s and probes). It is also near bge-small's 512-token limit, so longer runbooks get truncated.
- **k = 1.** Hit rate drops from 1.00 to 0.90. The agent reads the candidates and chooses, so returning 3 is cheap insurance.

## Consequences

- Runbooks must keep the `# RB-NNN: Title` and `## Section` format. `chunk_runbook` rejects files without the header.
- Hit@3 is 1.00 for every strategy, because with 8 runbooks the top 3 covers 3/8 of the corpus. **Hit@1 and MRR are the metrics that actually separate the strategies.** Revisit when the corpus grows past about 20 runbooks.
- Remaining top-1 confusions (each still rank 2): RB-001 vs RB-002 when the question gives no "already cleaned" cue, RB-003 vs RB-006 for hangs, and RB-003 vs RB-004 for w3wp faults. The diagnosis agent must check the "use RB-00X instead" lines in each Diagnosis section rather than trusting rank 1.
- `run_rag_eval.py --gate section` exits non-zero below the targets, ready to become a CI gate once CI has Qdrant and the model.
- Caveat: one author wrote both the questions and the eval, so the numbers are optimistic. Add real incident wording from the replay cases in phase 3 and later.
