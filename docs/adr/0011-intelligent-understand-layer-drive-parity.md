# 0011 — Intelligent Understand layer (Drive parity), not regex pummeling

- **Status:** Accepted — **implemented 2026-06-16**; default path **superseded 2026-06-17** by [ADR-0014](0014-simple-intelligent-puzzle.md) (Capture → Book → Verify)
- **Date:** 2026-06-16
- **Deciders:** Ledgr team

> **2026-06-17 amendment:** Understand is once again the **default** hot path
> (`LEDGR_UNDERSTAND_EXTRACT` defaults on). ADR-0014's two-call Capture → Book →
> Verify pipeline is retained as opt-in via `LEDGR_CAPTURE_BOOK=1` (used for SOA
> experiments and A/B only). Single-call Understand matches Google's recommended
> pattern (one `generate_content` with PDF `Part` + Pydantic schema) and lowers
> 503 risk. The `tax_visible_on_document` field on `DocumentLedgerExtract`
> propagates to `NormalizedInvoice` so the export layer keeps the no-invented-tax
> guarantee from ADR-0014 without a second LLM call. SOA packages alone use
> legacy `DocumentRecordBundle` + `document_normalizer.py`.

## Context

Ledgr's invoice lane previously used a **two-phase capture + reinterpret** pattern:

1. **Phase 1** — Gemini structured output into `DocumentRecordBundle`, instructed to
   capture every line item verbatim and *not* summarize.
2. **Phase 2** — ~700 lines of Python in `document_normalizer.py` (label frozensets,
   telco GST regex, invoice-number heuristics) to re-derive what a bookkeeper would
   have posted.

This fought the model: Telco Provider A bills dumped 150+ line items, broke Firestore session
limits, and still needed `_telco_ledger_lines` to collapse SR/ZR buckets. Sample Test Group
and other simple invoices worked in Google Drive's side panel but not reliably in
Ledgr.

Research against Google's Gemini document + structured-output guidance (and observed
Drive behaviour) showed we already had the right API — the wrong **task and schema**.
Drive's side panel is effectively **one multimodal call → human summary table +
accounting-meaningful structure**, not faithful OCR followed by regex.

ADR-0001 correctly rejects *per-step LLM graph nodes* (token burn). This ADR does
**not** reintroduce that. Intelligence moves into **one extract call inside the
Engine**, not into a chain of coordinator agents.

## Decision

Split document processing into three layers:

| Layer | Role | Technology |
|-------|------|------------|
| **Understand** | Read the PDF; return Drive-style summary + ledger lines | One Gemini call, `DocumentLedgerExtract` schema |
| **Policy** | Tax, COA, reconcile, client GST rules | Deterministic Python (unchanged) |
| **Commit** | HITL, export projection, workbook append | ADK graph + Slack (unchanged; ADR-0003/0007) |

### Understand (default path)

For **standard invoices, receipts, and telco/utility bills**:

- Single call: `extract_document_ledger()` → `DocumentLedgerExtract`
- Schema fields: `summary_table` (Category/Details pairs) + `ledger_lines` + headers
- Thin mapper → `NormalizedInvoice` (ADR-0005 canonical schema unchanged)
- Orchestrator: `process_invoice_document()` shared by graph nodes and eval harness
- Graph node: `extract_invoice_document_node` replaces separate extract + normalize

Feature flag: `LEDGR_UNDERSTAND_EXTRACT` (default **on**).

### Default path

`extract_document_ledger()` — single multimodal Gemini call returning
`DocumentLedgerExtract` (PDF `Part` + Pydantic schema). See ADR-0014 for the
tax-discipline export guarantees carried forward.

### ADR-0014 pipeline (opt-in)

Capture → Book → Verify via `LEDGR_CAPTURE_BOOK=1` (default off). See ADR-0014.

### Legacy path (retained)

For **SOA packages** only:

- `DocumentRecordBundle` + `document_normalizer.py` (Phase 1 + Phase 2)
- Routed when `doc_type == statement_of_account`

### Understand path (opt-out)

To force-off the default Understand path: `LEDGR_UNDERSTAND_EXTRACT=0`.
Routes then fall back to ADR-0014 capture-book or legacy SOA normalizer.

### Slack UX (ADR-0007 complement)

On approve / delivery, show the **ledger preview data_table** (where rows land)
and re-upload the FY workbook. The Understand layer still produces
`summary_table` in session state for eval and debug, but it is **not** posted
to Slack — the spreadsheet and ledger table are the source of truth.

### What stays deterministic (not "intelligent")

- `reconcile()` — line sums vs document total (CEL-equivalent math gate)
- `TaxClassifier` + client GST registration policy
- COA categorisation (deterministic-first + bounded LLM fallback)
- Per-target exporters (QBS / Xero)
- HITL gates and [[Correction]] learning (ADR-0004)

## Consequences

- **Better quality on invoice/telco lane** without growing the ADK graph. Verified
  locally: Sample Test Group Vendor Alpha PDF and Telco Provider A BV-0002830 bill produce correct
  summary tables and 1- or 2-line ledger shapes in one call.
- **Less code on the hot path** — invoice/telco bypasses Phase 2 regex; legacy
  normalizer kept only for SOA/claims.
- **Compatible with ADR-0001** — still one Engine authority, slim workflow, no
  per-step extraction LLM nodes.
- **Compatible with ADR-0005** — `NormalizedInvoice` remains canonical; exporters
  unchanged.
- **Pre-implementation research code** (`alf_engine`, `general_invoice_agent`,
  `investigate_agent_reconst`) archived under `legacy/` — not production Slack path.
- **Slack E2E verification** on the live runner: upload → thinking plan → ledger preview → approve → Excel.

## Alternatives considered

- **Keep Phase 1 + expand Phase 2 regex** — rejected; fights the model, does not
  match Drive, caused Telco Provider A/Firestore pain.
- **Multi-agent swarm per field** — rejected; pre-consolidation experiment burned
  tokens (ADR-0001 lesson).
- **Separate summary call then extract call** — deferred; single-call quality on
  Sample Test Group + Telco Provider A was sufficient; two-call variant remains eval fallback.

## Implementation pointers

- `invoice_processing/extract/ledger_extract.py` — schema + extract + mapper
- `invoice_processing/extract/process_invoice_document.py` — routing orchestrator
- `accounting_agents/nodes.py` — `extract_invoice_document_node`
- `app/blocks.py` — `summary_table_blocks`
- `accounting_agents/slack_runner.py` — ledger preview + workbook on delivery
