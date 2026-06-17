# 0014 — Simple Intelligent Puzzle (Capture → Book → Verify)

- **Status:** Partially superseded 2026-06-17 — export principles kept; Capture → Book → Verify demoted to opt-in
- **Date:** 2026-06-17
- **Deciders:** Ledgr team
- **Supersedes in part:** ADR-0011 default extraction path

## Context

Client A Slack runs (2026-06-17) exposed a class of failures that were **not**
primarily Gemini mis-reading PDFs. Downstream Python **reinterpreted** captures:

- `_tax_amount()` invented GST as `net × 9%` when `gst_amount` was missing
- `tax_hint` defaulted to `"SR"` on understand ledger lines
- `reconcile()` compared line sums to model-invented subtotals, producing false alarms
- Understand-as-default collapsed expense-claim rows before export

Drive/Gemini side panels behave as: **read faithfully → book with reasoning → verify math**.

## Decision

Adopt a four-layer pipeline for invoice/receipt lane:

| Layer | Role | Where it lives |
|-------|------|----------------|
| **Capture** | Faithful read — every visible row, footer totals, parties | `DocumentRecordBundle` (opt-in via `LEDGR_CAPTURE_BOOK=1`) |
| **Book** | Posting granularity, direction, `direction_reason`, `tax_visible_on_document` | `BookingProposal` (opt-in via `LEDGR_CAPTURE_BOOK=1`) |
| **Verify** | Arithmetic only — footer reconcile, tax visibility gate | `reconcile()` + `verify.py` |
| **Export** | Column projection — **never invent tax** | `TaxClassifier` + exporters |

### Default hot path (post-revision)

`extract_document_ledger()` — a **single multimodal Gemini call** that returns
`DocumentLedgerExtract` (PDF + Pydantic schema, no post-call book step). This
matches Google's recommended pattern for invoice extraction (one `generate_content`
with PDF `Part` + structured schema). The `tax_visible_on_document` field is
populated by the model and propagated to `NormalizedInvoice` so the export layer
keeps the no-invented-tax guarantee.

### Python must never

- Invent tax (`net × rate` backfill)
- Default `tax_hint` / treatment to SR when the document is silent
- Override direction with `if expense_claim` heuristics
- Reconcile against subtotals that were not captured on the document

### Feature flags

| Flag | Default | Meaning |
|------|---------|---------|
| `LEDGR_CAPTURE_BOOK` | `0` | Opt-in Capture → Book → Verify (this ADR's pipeline) |
| `LEDGR_UNDERSTAND_EXTRACT` | `1` | Single-call Understand path (default hot path) |

SOA packages remain on legacy `DocumentRecordBundle` + normalizer.

### Evidence schema

`DocumentRecord` fields carry optional `EvidenceRef` (`page`, `source`, `confidence`)
for critic grounding in `review_extraction_node`.

### Eval properties (nightly)

- `tax_not_invented` — export `TaxAmount=0` when `tax_visible_on_document=false`
- `footer_reconcile` — lines vs capture footer, skip absent subtotal/GST rows
- `groundedness` — booking cites capture parties for direction

## Consequences

- Expense reimbursements with no tax column export **No Tax**, not invented SR GST
- False reconcile alarms from mis-read subtotal cells are eliminated
- Default invoice lane is the Google-recommended single-call path (lower 503 risk)
- Capture → Book retained for A/B and SOA experiments via `LEDGR_CAPTURE_BOOK=1`
- `document_normalizer.py` frozen for SOA only; not on invoice hot path
- Deleted `accounting_agents/tools.py` (deprecated prototype, ADR-0013)

## References

- ADR-0011 (Understand layer — default)
- ADR-0013 (native ADK adoption matrix)
- `tests/test_puzzle_properties.py`
- `tests/eval/test_extract_properties.py`
