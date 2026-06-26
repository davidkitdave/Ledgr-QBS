# 0029 — Multi-document fan-out via deterministic page-window chunking; the LLM is never the authority on page boundaries

- **Status:** Accepted
- **Date:** 2026-06-26
- **Deciders:** Ledgr team
- **Relates to:** ADR-0011 (Understand layer), ADR-0026 (AI reads, rules apply); resolves the
  Stream A.3 read-layer item and records the architecture behind the PR #21 (issue #16) fix

## Context

One uploaded PDF can hold many logical documents: a Statement-of-Account cover page + N
invoices, or one page holding dozens of receipts. A single "extract all N documents in one
Gemini call" approach fails at scale — output-token truncation produces unparseable JSON, one
malformed item poisons the whole parse, and there is no per-item retry. **Issue #16** (large
multi-receipt PDF → `status=error`, 0 captured) was exactly this failure; **PR #21** fixed it
by chunking.

ADK + Gemini guidance (verified via the ADK-docs and Google developer-knowledge MCPs,
2026-06-26) is explicit: **"split long documents into multiple PDFs"** is the documented best
practice; structured-output lists are bounded by output-token caps; and the models **"are not
precise at locating text or objects within PDFs"** and may return **"approximated counts"** —
i.e. the LLM is unreliable at page-location and counting, and there is **no native
page-provenance field**.

## Decision

**Fan-out is deterministic chunking + per-chunk LLM understanding; the LLM is never trusted for
page boundaries or counts.**

1. **Deterministic Python** splits an oversized PDF into fixed page windows (default 5 pages),
   extracts each window with the normal `response_schema` call (bounded output ⇒ no
   truncation), and **merges** results, re-basing page numbers to the source PDF **in our own
   code** — provenance is owned by us, per Google's guidance; the LLM does not report it.
2. The **LLM owns content understanding only** ("this region is a receipt for vendor X
   totalling Y"); the **deterministic merge owns page math and orchestration**. This is the ADK
   division (`LlmAgent` reasoning vs deterministic workflow / `FunctionTool` controllers) and
   is **not** the brittle keyword if/else Branch C targets — it is honest plumbing with zero
   semantic guessing.
3. Every chunk emits a **failsafe output** (`skipped_documents` + `fallback_reason`) so a bad
   chunk surfaces rather than silently zeroing the batch (ADK fan-in "failsafe output"
   guidance).
4. **SOA cover-skip must survive chunking** — a statement cover detected in any chunk is
   dropped globally and never booked (the merge has no SOA awareness today; this is the A.3
   must-do).
5. The **straddler** (a single invoice crossing a chunk cut) is left to the existing
   reconcile → [[Review (HITL)]] safety net — a partial extraction fails reconciliation and is
   flagged — and we **measure before** investing in overlap/dedup or continuity-stitch
   ("measure before adding logic").
6. We **reject** LLM-driven boundary detection ("ask the model how many docs and which
   pages") — Google's documented spatial-reasoning limit makes it unreliable.

## Consequences

- Large multi-doc PDFs extract reliably (fixes the #16 family); failures are visible, not
  silent.
- The fan-out has **zero semantic if/else** — all meaning comes from the LLM, all page math
  from code.
- A rare straddler may pause for Review until measurement justifies more; SOA bundles need the
  cover-skip-through-chunking fix (A.3).

## Alternatives considered

- **Single big structured-output call** — truncates / poisons at scale (the #16 bug);
  rejected.
- **LLM segmentation (the LLM finds the boundaries)** — Google: the model is imprecise at
  locating/counting on a page; rejected.
- **Overlap windows + dedup now**, or **merge-time continuity stitch** — viable upgrades,
  **deferred** pending measurement of how often straddlers actually occur.

## Sources

- Gemini — document-processing best practice "split into multiple PDFs"; document-understanding
  spatial-reasoning limitation ("not precise at locating text or objects", "approximated
  counts"): `ai.google.dev/gemini-api/docs/document-processing`,
  `docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/document-understanding`
- Gemini — structured-output limits / truncation, `max_output_tokens`, flat schema:
  `ai.google.dev/gemini-api/docs/structured-output`
- ADK — workflow agents as deterministic controllers; fan-in `JoinNode` "failsafe output";
  dynamic `parallel_supervisor` (`asyncio.gather`, per-item retry on resume):
  `adk.dev/agents/workflow-agents/`, `adk.dev/graphs/routes/`, `adk.dev/graphs/dynamic/`
- ADK — provenance/large payloads belong in artifacts, not state: `adk.dev/graphs/data-handling/`
