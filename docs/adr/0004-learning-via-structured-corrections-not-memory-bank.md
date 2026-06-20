# 0004 — Learning via structured Firestore Corrections, not ADK Memory Bank

- **Status:** Accepted
- **Date:** 2026-06-13
- **Deciders:** Ledgr team

## Context

The Teammate should let a user say "this is wrong / this vendor belongs to account
X" and then **improve** so the same mistake doesn't recur. ADK (verified against the
MCP docs) offers a `MemoryService` tier, including **Memory Bank**
(`VertexAiMemoryBankService`), which uses an LLM to extract and semantically
consolidate "memories" from past conversations, retrieved via `load_memory`.

Two things must be kept apart:

- **Extraction defects** ("a column is blank / never captured") are **not** a memory
  problem. They are fixed in the Engine's extractor schema/prompt and verified by
  eval. Remembering cannot populate a field the extractor never produced.
- **Per-client mapping rules** ("vendor X → account 61010", "this vendor's GST is in
  the second column") are the actual unit of *learning*. They are **structured,
  keyed by vendor/field, and must be applied deterministically** by the Engine on
  the next document — not retrieved fuzzily by an LLM.

## Decision

Implement learning as a **structured per-client Correction store in Firestore**
(the planned `Entity_Memory`). A `remember_correction` tool writes a Correction;
the **deterministic Engine reads applicable Corrections before categorising /
extracting** the next document. ADK **Memory Bank is not used** for this.

## Consequences

- Corrections are deterministic and auditable: "vendor X = account Y" is *obeyed*,
  not probabilistically recalled.
- No dependency on a Vertex Agent Engine instance and no per-turn LLM retrieval cost.
- Corrections integrate cleanly with HITL (ADR-0003): an approve-with-edit becomes a
  Correction.
- We forgo semantic, natural-language cross-session recall. If that is ever wanted
  (e.g. "what did we discuss about this client last quarter"), Memory Bank can be
  added **alongside** the Correction store without disturbing it.

## Alternatives considered

- **ADK Memory Bank (semantic)** — good for fuzzy conversational recall, but cannot
  guarantee a hard "vendor X = account Y" rule, costs tokens per turn, and needs an
  Agent Engine; rejected as the learning mechanism.
- **Both (structured rules + Memory Bank)** — most capable, most moving parts;
  deferred to a later phase if natural-language recall is needed.

## Amendment — RAG suggestion layer as an optional input to Corrections (roadmap, 2026-06-18)

Entity resolution remains **deterministic Corrections as the authoritative
mapping**. This is an audit requirement: "vendor X = account Y" must be obeyed
exactly, not recalled probabilistically.

A future optional enhancement may add a **RAG / embedding suggestion layer** for
fuzzy vendor matching: when a new vendor arrives with no existing Correction, an
embedding similarity search over the client's prior Correction history (or COA
keyword index) suggests the most likely account code. The human confirms or edits
the suggestion. On confirmation, the confirmed mapping is written as a
**Correction** — it enters the deterministic, auditable store and is applied on
every subsequent document.

This reconciles the RAG idea with ADR-0004 without reversing it: RAG is a
**suggestion input**, not a replacement for the Correction store. The Correction
is still the authoritative output. RAG that is never confirmed produces no lasting
effect. See ADR-0019 (Universal Adapter, Phase 3) for the planned sequencing of
this enhancement relative to other delivery work.
