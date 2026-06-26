# 0028 — Accounting Module is a per-ERP projection; Payment status is a universal read field

- **Status:** Accepted
- **Date:** 2026-06-26
- **Deciders:** Ledgr team
- **Relates to:** ADR-0005 (canonical schema → per-target projection), ADR-0011 (Understand
  layer), ADR-0026 (AI reads, rules apply)

## Context

Real Malaysian ERPs (AutoCount, SQL Account) post a **paid** document to a **CashBook**
module — a **Payment Voucher (PV)** for a paid purchase, an **Official Receipt (OR)** for a
paid sale — and an **unpaid/credit** document to an **AP/AR Invoice** module. Today every
document, paid or not, books to AP/AR Invoice, so a paid retail receipt creates a phantom
creditor balance that never gets knocked off. **Xero and QBS Ledger do not have this split** —
a bill is posted and payment is reconciled separately.

The system already separates **Direction** (purchase/sales) — every target's profile has
purchase/sales columns. It does **not** model **whether a document was paid**, and there is no
CashBook exporter.

## Decision

1. **Payment status** (`paid | credit | unknown`) is a property of the **document**, read
   **once** at the document boundary into the [[Canonical Schema]] — universal, identical
   across targets ("one understanding → many exports", ADR-0005). It is not captured
   per-ERP.
2. **Accounting Module** — where the document lands — is a **per-target projection** expressed
   in **each target's YAML profile** (rule-data), not in code:
   - AutoCount / SQL Account profiles declare a **CashBook block** ⇒ exporter routes
     `paid ⇒ PV/OR`, `credit ⇒ AP/AR`.
   - QBS / Xero profiles declare **no CashBook block** ⇒ exporter ignores payment status and
     posts to the Direction sheet (today's behaviour).
   The routing Python is generic ("read the profile") and contains **no ERP names**.
3. The change is **end-to-end but profile-driven**: the canonical `payment_status`, the
   exporter, the Excel [[Workbook]] sheets/tabs, the Slack preview columns and the Job summary
   all **follow** the profile; the [[Completeness Contract]] gains the CashBook module's
   required headers for the targets that declare it.
4. **Sequencing:** **Direction ships first** (Phase A, universal); the **Module / CashBook fork
   is Phase B**, gated on capturing `payment_status` + building PV/OR exporters + golden tests,
   behind the eval gate.

## Consequences

- Paid receipts can finally route to CashBook PV/OR for AutoCount/SQL, eliminating phantom
  creditor balances — **without touching** Xero/QBS behaviour.
- Adding or altering a module is a YAML edit; the export, Excel and Slack surfaces follow
  automatically.
- Until Phase B lands, paid purchases still book to AP Invoice (today's behaviour) — a known,
  bounded interim, not a new regression.

## Alternatives considered

- **One term for Direction and Module** — collapses paid-vs-credit and mis-routes paid
  receipts; rejected (this is the exact gap auditors catch).
- **Capture `payment_status` only for AutoCount/SQL clients** — breaks "one understanding →
  many exports" (same document read differently per client); rejected.
- **Hardcode CashBook routing in Python keyed on the ERP name** — violates Branch-C /
  dynamic-by-data; rejected.

## Sources

- ADK / Gemini — structured output as the read layer; rule-data in YAML / Skill `assets/`
  (see ADR-0026 sources). Verified via ADK-docs + Google developer-knowledge MCPs (2026-06-26).
