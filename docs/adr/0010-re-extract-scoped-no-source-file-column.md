# 0010 — `re_extract_document` re-runs through HITL; no source-file workbook column

- **Status:** Accepted
- **Date:** 2026-06-15
- **Deciders:** David (developer)
- **Relates to:** ADR-0002 (Slack as system of record), ADR-0009 (chat write tools), Step 7 of the intelligent-agent master plan

## Context

Master-plan Step 7 includes a chat tool `re_extract_document(file_id, hints)` — "re-read
the Acme invoice as a credit note" — that should re-process an already-filed document and
**replace** its ledger rows. Designing it surfaced a hard data-model fact (architecture
review, 2026-06-15):

- The FY workbook rows are **anonymous**. The QBS/Xero/bank column sets
  (`exporters.py`) carry **no source-file id or `doc_key`** on a row. (The Xero exporter
  docstring mentions a "Source File ID" but it is NOT in the actual column list — stale.)
- Provenance lives **only** in Firestore: the ledger pointer's content-based
  `seen_doc_keys` set (`ledger_store.py`). `doc_key = "{sheet}:{invoice_number}"`
  (`nodes.py` `consolidate_node`) — it is not keyed by `file_id` and is never written into
  the workbook (the Excel is deliberately clean — `ledger_store.py:16-17`).

So given a `file_id`, there is **no index** `file_id → rows`. You can only address rows by
**content signature** (Vendor + Invoice Number + Date + Amount) or by **month** (the `Date`
column). A truly surgical "replace exactly the rows from file X" is unsatisfiable without
adding provenance to the schema.

## Decision

1. **Do NOT add a source-file / `doc_key` column to the workbook.** It would break the
   "clean, human-readable Excel" contract, change the exporter output format the user's
   accounting software ingests (per-target column sets are fixed), and force changes
   across every read tool, `read_rows`, `_row_signature`, the bank-rebuild path, and both
   exporters — a whole-I/O-layer blast radius for an occasionally-used feature.

2. **`re_extract_document` re-runs through the existing pipeline + HITL, and removes the
   stale rows by reconstructed identity:**
   - Re-download the PDF by `file_id` (`download_pdf_bytes`), re-run the **same** document
     pipeline with the hint seeded into run state (`review_hint` → `_reextract_with_hint`
     / `extract_invoice_node`). The corrected read flows through the **normal Approve /
     Edit / Reject card**, so a human confirms it — no new extraction logic, and §0.5-C
     tax is applied by the canonical classifier for free.
   - Before re-appending, remove the old rows whose **reconstructed identity** (sheet +
     invoice number, → `doc_key`) matches, and **purge that `doc_key`** from
     `seen_doc_keys` (else the re-drop is silently deduped). Reconstruct the key with the
     exact normalization `consolidate_node` uses.

3. **Honest limitation, surfaced not hidden.** Surgical replacement is reliable only when
   the re-extraction **preserves the document identity** (a re-categorisation / tax fix —
   same invoice number). When the hint **changes** the identity (e.g. "read as a credit
   note" splits the document), the old rows cannot be auto-located: removal matches 0 rows
   and the tool tells the user to use **`replace_recorded_month`** (ADR-/Step-7, ships
   first) or `remove_ledger_row` explicitly — it must **never silently double-record**.

4. **Gating + execution reuse ADR-0009:** two-turn Tool Confirmation, deterministic
   spec re-derivation on commit, a dedicated `pending_reextract` drain that has the
   **document runner injected** (the chat lane otherwise only holds the chat runner), and
   the per-`fc_id` idempotency + per-channel ledger lock.

## Consequences

- `re_extract_document` delivers the "talk-to-it-and-it-re-reads" experience for the
  common case (fix how a doc was coded) without a schema change, and degrades safely +
  loudly for the identity-changing case by delegating to the month-level primitive.
- The workbook stays clean and the exporter formats stay stable.
- The marginal cost is one extra pipeline run on the long-tail "redo this" path —
  bounded and only on explicit user request.

## Alternatives considered

- **Add a hidden source-file column** — rejected (§Decision 1): blast radius across the
  whole I/O + exporter layer for a clean surgical replace that isn't worth it.
- **Re-append the corrected version and orphan the old rows** (remove only the `doc_key`
  from `seen_doc_keys`, leave the stale rows) — rejected: double-counts in every P&L / GST
  figure; "the old rows remain" is unfaithful to "act on the books".
- **Content-signature removal as the primary mechanism** — kept as the identity-match path,
  but it is fuzzy when identity changes, so it is paired with the explicit fallback rather
  than trusted blindly.
