> **Archived 2026-07-01** — Describes the removed `accounting_agents` / `invoice_processing` graph and factory. **Live runtime:** `ledgr_slack` + `ledgr_agent` ([ADR-0032](../0032-ledgr-agent-and-slack-two-packages.md)). History only; do not implement against this doc.

# 0025 — Faithful multi-document extraction & COA confidence (WS-0/2/3)

> **ℹ️ Amended by [ADR-0026](0026-ai-reads-rules-apply-on-a-lean-llmagent.md) (2026-06-24).**
> The faithful-extraction and COA-confidence decisions stand. *Superseded:* §WS-0.4's choice
> of the ADK `RequestInput` graph node as the COA HITL primitive — COA review now flows through
> the batch tool's `pending_reviews` + the `hitl.py` Firestore bridge (graph retired).

- **Status:** Accepted (2026-06-21) — §WS-0.4 HITL primitive superseded by ADR-0026
- **Date:** 2026-06-21
- **Deciders:** Ledgr team
- **Plan:** `docs/superpowers/plans/2026-06-21-intelligent-extraction-implementation.md`
- **Research:** `docs/superpowers/specs/2026-06-21-intelligent-extraction-research.md`

## Context

The intelligent-extraction plan (2026-06-21) requires two build-time verifications
before shipping WS-2 (faithful array extraction) and WS-3 (COA trustworthiness):

1. **Enum-in-nested-array on Vertex Flash-Lite** (WS-0.3)
2. **ADK Tool Confirmation vs Firestore session service** for COA HITL (WS-0.4)

## WS-0.3 — Enum structural gate (Vertex Flash-Lite)

**Verified:** `scripts/spike_vertex_enum_nested_array.py` on Vertex
`gemini-2.5-flash-lite@us-central1` — **0 / 108 out-of-set emissions** with 158
synthetic enum keys in a nested `lines[]` schema.

**Decision:** Enum constraint is a **structural validity guarantee** on Vertex
Flash-Lite. Post-validation of code membership is a semantic-plausibility check only.

**Still required:** `UNMAPPED` sentinel (or nullable) in the COA enum — the model
picks an in-set code even for nonsense lines; enum guarantees validity, not
correctness. See spike doc:
`docs/superpowers/spikes/2026-06-21-vertex-flash-lite-enum-nested-array.md`.

**Region note:** Flash-Lite is not served in `asia-southeast1`; prod LITE tier
uses `gemini-2.5-flash` in-region. Enum mechanism is API-config-level; behaviour
is expected to hold across model ids in the same API family.

## WS-0.4 — COA HITL primitive (Tool Confirmation vs RequestInput)

**Question:** Can WS-3.5 use ADK Tool Confirmation (`require_confirmation=threshold_fn`)
with our **Firestore session service** for flagged/UNMAPPED COA lines?

**Answer: No — use `RequestInput` in the slim graph (ADR-0003).**

| Primitive | Firestore sessions | Fits document pipeline graph |
|-----------|-------------------|------------------------------|
| **RequestInput** node | ✅ Proven (`hitl.py`, `approval_gate`) | ✅ |
| **Tool Confirmation** | ❌ ADK docs exclude `DatabaseSessionService` / persistent backends | ❌ Bare `LlmAgent` only |

Tool Confirmation **is** used on the **chat lane** for write tools (ADR-0009) with
in-memory eval sessions. Production document HITL and COA reviewer picks must use
the existing **RequestInput + Slack resume** path — not Tool Confirmation.

**WS-3.5 implementation:** Route flagged/UNMAPPED COA lines through
`approval_gate` / review card; human picks from `alternative_codes[]`; resume
applies the chosen code via `hitl.py`.

## WS-2 — Faithful single-schema multi-document extraction

**Decision:** Replace the understand-path **list-of-1 wrap**
(`process_invoice_document.py` single `DocumentLedgerExtract` → one
`NormalizedInvoice`) with **`documents: list[ExtractedDocument]`** — one Gemini
call per file, each element carrying verbatim fields + `page_range`.

**G-gates (must pass before ship):**

- **G1** per-doc reconcile
- **G2** page-coverage (union == pages, no gaps/overlaps)
- **G3** doc-count on delivery card
- **G4** tolerance per currency (integer cents)
- **G5** partial-failure semantics (deliver good docs, flag gaps loudly)

Eval gate: G-cluster in `tests/eval/datasets/ledgr.evalset.json` (G1–G4).

## WS-3 — COA confidence & abstention

**Thinking:** OFF on default path (spec §10).

**Four levers:**

1. Deterministic spine + LLM matcher only (`categorizer.py`)
2. In-context structured call (no RAG at current COA scale)
3. **`UNMAPPED` sentinel** in enum + abstention prompt
4. **Logprob gate** (`responseLogprobs`, `avgLogprobs`, top-1→top-2 margin) —
   self-reported `confidence` advisory only

**Scale threshold for embeddings/RAG:** thousands of COA codes (record when reached).

## Consequences

- WS-2 and WS-3 can proceed; build-time blockers are closed.
- COA HITL reuses ADR-0003 infrastructure — no new ADK primitive.
- Enum schema is safe on Vertex; semantic correctness still needs reconcile +
  logprob gate + human flag.

## Alternatives considered

- **Tool Confirmation for COA HITL** — rejected (Firestore incompatibility).
- **RAG for COA at ~158 codes** — rejected (premature; in-context enum sufficient).
