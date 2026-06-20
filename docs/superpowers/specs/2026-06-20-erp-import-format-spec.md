# Spec: AutoCount + SQL Account real import-format profiles (closes WS5 ship-gate)

Status: Ready for execution — 2026-06-20
Author: lead (Opus), from UltraQA reverse-engineering of the real templates
Branch: `feat/ledgr-my-sst-correctness`
Closes: the WS5 golden-file ship-gate in `docs/superpowers/plans/2026-06-20-ledgr-jurisdiction-sst-multierp.md`

## Problem
The WS5 exporter **engine** is correct (deterministic code resolution, client-master-wins,
creditor resolution, unmapped Slack flag, rate-aware tax codes SV-8/SV-6/SV). But the profile
**column layouts** in `invoice_processing/shared_libraries/erp_profiles/{autocount,sql_account}.yaml`
are seed placeholders that do NOT match the real AutoCount/SQL import templates, so the import
wizard would reject the file. The real templates are now in
`~/Desktop/LocalTest/header template/{Autocount Template, SQL Header}/`. This spec encodes their
exact format so we emit importable files.

Reverse-engineered facts (verified against the real `.xls/.xlsx` templates 2026-06-20):

### AutoCount — separate file per doc type, single sheet, master+detail flat in one row
Tax model: **AutoCount computes tax itself** from `TaxType` × `TaxableAmt`. We do NOT supply a tax
amount column. `DocNo` = `<<New>>` (AutoCount auto-numbers); the supplier's own invoice number goes
in `SupplierInvoiceNo` (AP only). `InclusiveTax` = `F` (we always pass tax-exclusive net).

- **AP Invoice** — sheet name `AP Invoice`. Columns (★=mandatory):
  `DocNo★ | DocDate★ | CreditorCode★ | SupplierInvoiceNo★ | JournalType★ | DisplayTerm | PurchaseAgent | Description | CurrencyRate | RefNo2 | Note | InclusiveTax | AccNo★ | ToAccountRate | DetailDescription | ProjNo | DeptNo | TaxType | TaxableAmt | TaxAdjustment | Amount★`
- **AR Invoice** — sheet name `AR Invoice`. Same shape, but `DebtorCode★` replaces CreditorCode+SupplierInvoiceNo, adds `CurrencyCode` (col 8), `SalesAgent` replaces PurchaseAgent. Columns:
  `DocNo★ | DocDate★ | DebtorCode★ | JournalType★ | DisplayTerm | SalesAgent | Description | CurrencyCode | CurrencyRate | RefNo2 | Note | InclusiveTax | AccNo★ | ToAccountRate | DetailDescription | ProjNo | DeptNo | TaxType | TaxableAmt | TaxAdjustment | Amount★`

Field mapping (per line → one row):
- `DocNo` = constant `<<New>>`
- `DocDate` = invoice_date (DD/MM/YYYY)
- `CreditorCode`/`DebtorCode` = resolved creditor/debtor code (blank if unmapped → flagged)
- `SupplierInvoiceNo` (AP) = invoice_number
- `JournalType` = constant `PURCHASE` (AP) / `SALES` (AR)
- `Description` + `DetailDescription` = line.description
- `CurrencyCode` (AR) = inv.currency; `CurrencyRate` = fx_rate (blank → AutoCount uses 1)
- `InclusiveTax` = constant `F`
- `AccNo` = line.account_code (GL) — blank if unmapped → flagged
- `TaxType` = resolved ERP tax code (SV-8/SV-6/…); blank for NT/OS
- `TaxableAmt` = line net (the base AutoCount taxes); blank when TaxType blank
- `Amount` = line net (sign-flipped for credit notes)

### SQL Account — separate file per doc type, sheet `SLPH_Invoice_Cash_Debit_Credit`, master+detail flat
Header is on row 6 (rows 1–5 are the color-key legend + notes — reproduce them OR emit just the
header row; confirm against a known-good import — start with header-on-row-1 unless the client file
shows the legend rows are required). Tax via `_TAX` (code) + `_TAXAMT` + `_AMOUNT`. Required (yellow):
`DOCNO, DOCDATE, CODE` (master) + `_ACCOUNT, _DESCRIPTION, _QTY, _UOM, _UNITPRICE` (detail).
The template already carries `Source File ID / [AI Status] / [AI Note]` trailing columns — these are
OUR automation columns (mirror the Xero exporter's `Source File ID`/`[AI Status]` pattern).

Emit at minimum the REQUIRED columns + core tax + AI columns; leave e-invoice (pink) fields as
present-but-empty headers. Field mapping (per line → one row):
- `DOCNO` = invoice_number (or `<<New>>` if SQL auto-numbers purchases — default to invoice_number)
- `DOCDATE` = invoice_date (DD/MM/YYYY)
- `CODE` = resolved creditor (purchase) / debtor (sales) code — blank if unmapped → flagged
- `DESCRIPTION` (master) + `_DESCRIPTION` (detail) = line.description
- `_ACCOUNT` = line.account_code — blank if unmapped → flagged
- `_QTY` = constant `1`
- `_UOM` = constant `UNIT`
- `_UNITPRICE` = line net (sign-flipped for CN)
- `_TAX` = resolved SQL tax code (SV/ST5/SVE/…); blank for NT/OS
- `_TAXAMT` = tax amount (when registered + visible); blank otherwise
- `_AMOUNT` = line net
- `Source File ID` / `[AI Status]` / `[AI Note]` = our provenance fields (as in XeroLedgerExporter)

## Required renderer changes (`ProfileLedgerExporter`, exporters.py)
The current renderer does flat `column → context-key` mapping only. Extend the profile schema +
renderer to support:
1. **Constant field values** — e.g. `JournalType`, `InclusiveTax`, `DocNo: "<<New>>"`, `_QTY: 1`,
   `_UOM: "UNIT"`. Profile syntax: a `constants:` map per doc-type, applied after field mapping.
2. **Per-doc-type sheet name** — `purchase_sheet: "AP Invoice"`, `sales_sheet: "AR Invoice"`
   (AutoCount); SQL both = `SLPH_Invoice_Cash_Debit_Credit`. Default keeps `Purchase`/`Sales` for
   qbs/xero. NOTE: AutoCount imports AP and AR as **separate files**; either emit two workbooks or
   keep two sheets and document that the user imports each sheet separately. Decide in impl; the
   simplest correct path is two sheets named exactly `AP Invoice`/`AR Invoice` and let the user
   point AutoCount's import at the right sheet. Confirm in live/golden test.
3. **New context fields** in `_row_context`: `supplier_invoice_no` (=invoice_number),
   `unit_price` (=net), `qty`, `uom`, plus `source_file_id`/`ai_status`/`ai_note` if available.
4. **Tax-model awareness**: AutoCount has NO tax-amount column (TaxableAmt carries the base); SQL
   has `_TAXAMT`. The existing `_tax_amount` already returns 0 when not registered — fine. For
   AutoCount, map net→`TaxableAmt` and net→`Amount`; do not emit a separate tax column.

## Acceptance tests (golden-file)
Add `tests/test_erp_golden_format.py`:
1. Generate an AutoCount purchase export; assert the `AP Invoice` sheet header **exactly equals**
   the real template's field row (read `~/Desktop/LocalTest/header template/Autocount Template/Import-AP-Invoice.xls`
   row index 2, cols 1..21). Same for AR vs `Import-AR-Invoice.xls`.
2. Assert every ★mandatory AutoCount column is non-empty for a fully-mapped line.
3. Generate a SQL purchase export; assert header contains every REQUIRED (yellow) column from
   `Import Purchase Invoice.xlsx` row 6, in order, and that `_QTY/_UOM/_UNITPRICE/_ACCOUNT` are
   populated. Same for sales.
4. Keep the existing synthetic WS5 tests green (tax code values SV-8/SV-6/SV unchanged).
5. Use the JBI master data as a realistic fixture: `COA & List.xlsx` → Party List "Mapping Code"
   column = creditor/debtor code (e.g. `400-A0001`), COA sheet = account codes. Build an
   EntityMemoryEntry set from it and assert a known JBI vendor (ATOM AUTO SUPPLY SDN BHD →
   400-A0001) resolves its code into `CreditorCode`/`CODE`.

Templates are blank format templates, not a client-imported known-good file — so this closes the
format gate but the FINAL acceptance (zero rejected rows) still = importing into the client's
AutoCount/SQL test company (per plan Verification step 4). Flag that in the PR.

## Out of scope (this pass)
Credit-note / debit-note / payment templates (AP/AR-Credit-Note etc.), CashBook/bank import,
full MyInvois e-invoice field population, petroleum specific-rate sales tax. Invoices only.
