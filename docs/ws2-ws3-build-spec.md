# WS2 + WS3 — Build Spec (extraction completeness + per-client export format)

> Branch: `phase1-feedback`. Target templates in-repo: `invoice_processing/data/export_demo/{qbs_ledger_FY2025.xlsx, xero_ledger_FY2025.xlsx}`.
> Principle: minimal, surgical edits. Do NOT change the model id/version or unrelated code. Keep the full suite green. No pytest asserts on LLM output (prompt-quality goes to eval, not pytest).

## Context (verified, with file:line)
- `export/exporters.py`: `get_exporter(system)` → `XeroLedgerExporter` / `QbsLedgerExporter` (216–222); default software `"QBS Ledger"` (`client_context.py:76`).
- Xero columns `_XERO_PURCHASE` (163–169) / `_XERO_SALES` (170–177): `*`-prefixed = Xero-required. `Total` column exists but is **never set** in `_xero_common()` (181–200) → blank in output.
- QBS columns `purchase_cols` (112–116) / `sales_cols` (117–120): per-line `Total`/`Total Amount` = `net + tax` (136, 153) — already repeats per row.
- `export/models.py`: `NormalizedInvoice` has **no invoice-level total** (only per-line `net_amount`/`gst_amount`); fields `invoice_date`/`due_date`/`invoice_number`/`currency`/`supplier`/`customer`/`reconciled`/`reconcile_note` (57–84). `InvoiceLine.account_code` filled by categorizer.
- `extract/invoice_extractor.py`: `ExtractedInvoice` DOES carry `subtotal/gst_total/total` (35–48) but `to_normalized()` (158–197) drops the doc total. Extraction prompt asks invoice_date "if determinable" (78) — not mandated; `due_date` rarely returned.
- Flag seam: `process_document()` after categorization (~250) before routing (~256); `reconcile()` returns `(ok, detail)` and only checks totals, not missing dates.

## WS3 — Export format correctness
1. **Xero `Total` per-line rule:** populate `Total` on EVERY line row with the **invoice-level total** (= Σ line `net_amount` + Σ line tax). Implement once per invoice in `XeroLedgerExporter.rows()` (or pass into `_xero_common`). A 2-line 2k invoice → both rows show `Total = 2000`.
2. Confirm QBS already correct (per-line `net+tax`); keep header order exactly matching the demo `.xlsx` templates for both software.
3. Prefer the **authoritative doc total** (`ExtractedInvoice.total`, carried via WS2 step 1) when present; fall back to Σ lines. Reconcile guard already flags mismatches.

## WS2 — Schema-complete extraction or flag
1. **Carry the doc total through:** add invoice-level `subtotal`/`gst_total`/`total` to `NormalizedInvoice` (or a single `total`) and populate in `to_normalized()` from `ExtractedInvoice`. Used by WS3 + validation.
2. **Mandate dates in extraction:** update the extraction prompt to always return `invoice_date` and `due_date` (drop "if determinable"); if `due_date` is genuinely absent, derive from stated terms if present, else leave None (→ flagged below). Prompt change only — validate via eval, not pytest.
3. **Per-exporter required-field set:**
   - Xero: the `*` columns — ContactName, InvoiceNumber, InvoiceDate, DueDate, Quantity, UnitAmount, AccountCode, TaxType.
   - QBS: Invoice Number, Invoice Date, Vendor/Customer Name, Sub Total/Amount, Total, Account Code / COA.
   Expose as a property on each exporter (e.g. `required_fields(doc_type)`), driven off the `*` markers for Xero.
4. **Validate + flag (don't emit half-filled silently):** after categorization in `process_document`, check the export-required fields for the client's software. If any missing → set `reconciled=False` and append a clear `note`, e.g. `needs review: missing due_date, account_code (line 2)`. The row is STILL written (data not lost) but the doc is flagged so WS1's Slack message surfaces it. *(Default — confirm with founder if flagged docs should instead be held out of the import file.)*

## Tests (deterministic pytest only)
- Xero exporter: every row of a multi-line invoice has `Total` = invoice total.
- QBS exporter: per-line total unchanged; header order matches template.
- `to_normalized`: doc total carried from `ExtractedInvoice`.
- Required-field validator: a NormalizedInvoice missing invoice_date / due_date / a line account_code → `reconciled=False` + note names the missing fields; complete invoice → unaffected.
- Full suite stays green.

## Out of scope (later workstreams)
- Slack ack/progress/rich-completion = WS1 (next).
- Single consolidated workbook + idempotency = WS4. Folder structure = WS5.
- Full HITL approval gate = Track A (ADK 2.0).
