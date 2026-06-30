# ADR-0034: Schema-as-prompt extraction for `ledgr_agent`

**Status:** Accepted  
**Date:** 2026-06-29  
**Related:** ADR-0026 (LLM reads, deterministic applies), ADR-0031 (light path), ADR-0033 (reference-free eval)

## Context

The `read_doc` tool sends a PDF to Gemini with a free-text prompt (`BUNDLE_READER_INSTRUCTION`) plus a structured output schema (`ReadDocumentBundle`). The old prompt enumerated document types with per-type rules — invoices, tax invoices, receipts, credit notes, bills, SOA packs, telco bills — and wrote purchase/sales logic in prose.

We want general self-classifying extraction like the Gemini Drive side-panel demo: the model identifies what kind of financial document it is without being told, and returns controllable structured output. We do **not** want hardcoded per-type branches in Python or long per-type rules in the prompt.

Research was conducted via the Google Developer Knowledge MCP and ADK docs MCP (2026-06-29).

## Research findings (with sources)

### 1. Enum for classification

Define document type as an enum (a fixed list). The model picks which type it is by itself, but cannot invent random types. Google's example uses `class DocumentType(str, Enum): INVOICE / RECEIPT / CREDIT_NOTE / BANK_STATEMENT / UNKNOWN`.

Source: Google Developer Knowledge `answer_query` (financial document extraction design).

### 2. Schema field descriptions act as the prompt

Google's structured-output docs state that the `description` on each schema field "acts as a prompt for the model, guiding it on what to extract" and that "the model uses field names to form its underlying prompt." Extraction guidance belongs in field `description`s, not in free-text rules. Use semantically critical field names (e.g. `tax_amount`, not `tax1`).

Source: `ai.google.dev/gemini-api/docs/generate-content/structured-output` — "Clear descriptions," "Strong typing + enum."

### 3. Minimal free-text prompt

The free-text prompt should state role + task + ambiguity handling only. Example: "You are a factual data extractor. Classify the provided document and extract key financial data."

Source: same structured-output docs; Google Developer Knowledge `answer_query`.

### 4. Validate semantics in application code

Structured output (`response_schema` / `output_schema`) guarantees syntactically correct JSON, **not** semantically correct values. Arithmetic checks (lines sum to subtotal, subtotal + tax = grand total) belong in eval metrics, never as hardcoded repair fallbacks in export code.

Source: structured-output docs — "Validation ... always validate values in your application"; ADK [Custom Metrics](https://adk.dev/evaluate/custom_metrics/index.md).

### 5. GEPA optimizes instruction only

ADK `GEPARootAgentPromptOptimizer` rewrites an agent's `instruction` only. It cannot edit schema field descriptions. Schema redesign is manual; GEPA is secondary polish on the short instruction.

Source: ADK [Optimization](https://adk.dev/optimize/index.md); `gepa_root_agent_prompt_optimizer.py`.

## Decision

Adopt **schema-as-prompt** for `ledgr_agent` extraction:

| Area | Choice |
|------|--------|
| `file_kind` | Keep enum `{bank_statement, commercial_documents}` — structural split (transactions vs line items). |
| `document_kind` | Enum `{invoice, receipt, credit_note, statement_of_account, other}` — model classifies from the page. |
| `doc_type` | Enum `{purchase, sales}` — model decides from issuer vs Bill-To layout. |
| Prompt | Replace long `BUNDLE_READER_INSTRUCTION` with a minimal general prompt (role + read-only + reconcile). |
| Schema | Move extraction guidance into field `description`s on `ReadDocument`, `Line`, `BankAccount`, `BankTxn`. |
| Credit notes | `credit_note` in `document_kind` enum (re-added 2026-06-30 for SOA+credit-note packs). Sign/reversal posting in export is follow-up. |
| Amounts | Read exactly as printed. Remove `_doc_sign` / `_signed_amount` sign-flip in `ledgr_agent/internal/export.py`. |
| Export repairs | Remove LLM-repair fallbacks (header-tax-into-line, net-from-total). Arithmetic validated by `extraction_self_consistency` metric only. |
| Eval harness | Doc-type-agnostic prompt (`"Process this document..."` not `"Process this invoice..."`). Add `extraction_classification` metric. Cases: 4 invoices + 1 receipt + 1 bank statement (6 total). |
| GEPA | Standalone `extraction_agent` in `ledgr_agent/eval/optimize/` as optimization target. Run once on faithfulness metric across all doc types. Review for overfitting before porting. |

Production orchestration unchanged: `read_doc` (tool) → `build_sheets` (tool).

## Consequences

- The Pydantic schema becomes the **primary extraction-quality lever**; GEPA's surface shrinks to the short instruction.
- `other` in `document_kind` is the escape hatch — the model never forces a wrong type.
- Telco bills, tax invoices, and multi-line bills are all `invoice`; no special-case handling.
- Legacy `invoice_processing/export/exporters.py` keeps its own `_doc_sign` (hermetic engine); only the light-path `ledgr_agent/internal/export.py` changes.
- `CONTEXT.md` gains a [[Schema-as-prompt extraction]] term cross-referencing this ADR.
- Status flips from **Proposed** to **Accepted** when the eval gate passes (overall ≥ 0.9 across 6 cases × 5 metrics).

## Summary-scope and generic tax breakdown (2026-06-29 extension)

### Research grounding

**Summary-scope for large bills.** Gemini reads many pages natively; over-extraction on telco-style bills came from prompt wording ("every printed field" / one row per charge) that encouraged transcribing appendix usage-detail rows. Fix: instruction + schema design — steer the model to extract **summary charge-breakdown lines** and ignore detail appendix pages. Single-call instruction change (no two-pass workflow).

Source: `ai.google.dev/gemini-api/docs/generate-content/document-processing` — native vision; system instructions steer summary vs noise.

**Generic tax breakdown.** Model tax as a list of `TaxComponent` objects with a **string** treatment label (not an enum) + rate % + taxable amount + tax amount. Mirrors Google/UBL `TaxSubtotal` (`TaxableAmount` / `TaxAmount` / `Percent`). SG "Standard-Rated 9%" / "Zero-Rated 0%", MY SST, and VAT all work without country hardcoding.

Source: Google Developer Knowledge `answer_query` — `TaxComponent{tax_treatment_label, tax_rate_percent, taxable_amount, tax_amount}`.

### Schema additions

| Field | Location | Purpose |
|-------|----------|---------|
| `TaxComponent` | `schemas.py` | One printed tax treatment: label, rate, taxable, tax |
| `Line.tax_treatment` | per line | Printed treatment label; flows to ERP `tax_code` column |
| `ReadDocument.tax_breakdown` | document header | One entry per distinct printed tax treatment |

### Prompt additions (`BUNDLE_READER_INSTRUCTION`)

- Summary-scope: when a document has summary + appendix pages, extract only summary charge lines (charge categories, not raw usage rows).
- Tax-breakdown: fill `tax_breakdown` per printed treatment; tag each line's `tax_treatment` from the page.

### Export plumbing

- `ledgr_agent/internal/export.py` `_line_context`: `tax_code` from `line.tax_treatment` (was always `""`).
- Xero `*TaxType`, AutoCount `TaxType`, SQL `_TAX(10)` pick it up via existing profile maps.
- QBS: added `Tax Treatment` column (Ledgr-added; native QBS has none).

### Eval extension

- New synthetic cases: `telco_multipage_bill` (summary + detail appendix trap), `sg_invoice_sr_zr_split` (SR 9% + ZR 0% breakdown).
- `score_self_consistency_on_extraction`: when `tax_breakdown` present, check component sums ≈ `tax_total` / `subtotal` (tolerant `_money_close`).
- Gate target: overall ≥ 0.9 across **8 cases** × 5 metrics.

See [ADR-0035](0035-bookable-row-granularity-metadata-first.md) for bookable-row granularity, metadata-first Slack delivery, eval bidirectional metrics (`extraction_itemized_fidelity`), and the Google-researched prompt improvement loop.

## SOA type and multi-invoice extraction (2026-06-29 extension)

### Problem

Real MY debtor-statement PDFs (e.g. COOL POWER: 1 statement page + 10 full invoice pages; ECBA SOA+invoice: 1 statement + 1 invoice) bundle multiple logical documents in one file. The model must return **each full invoice as its own document** and skip the statement page when invoices are attached; when only the statement is present, classify it as `statement_of_account`.

### Decision

| Area | Choice |
|------|--------|
| `document_kind` | Add `statement_of_account` enum value for debtor statements / SOAs. |
| Bundled PDFs | `invoices_only`: extract attached full invoices; do not return the statement page. |
| Standalone SOA | One `ReadDocument` with `document_kind=statement_of_account`; listed invoices as lines. |
| `build_sheets` | Skip `statement_of_account` documents (not postable to ERP). |
| Summary-scope | Tightened: applies to detail rows within one bill, not separate invoices bundled with a statement. |
| Eval | `standalone_soa` (count=1, kind=SOA) + `soa_plus_invoices` (count=3, kind=invoice); `expected_document_count` in classification metric. |
| Real validation | COOL POWER + ECBA PDFs from LocalTest; output in gitignored `artifacts/grade_results/`. |
