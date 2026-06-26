# 0028 — Accounting Module is a per-target projection driven by the counterparty code; payment is bank-lane settlement

- **Status:** Accepted (Amended 2026-06-26)
- **Date:** 2026-06-26
- **Deciders:** Ledgr team
- **Relates to:** ADR-0005 (canonical schema → per-target projection), ADR-0006 (client's own
  COA is authority, no baked-in default), ADR-0011 (Understand layer), ADR-0026 (AI reads, rules
  apply), ADR-0027 (Direction read + vendor-role floor)

## Amendment note (2026-06-26)

This ADR originally framed the Accounting Module as a function of **Direction × Payment status**,
with `payment_status` (paid|credit|unknown) read once at the document boundary as a universal
field. Studying a **real SQL Account client's full audit pack** (Trial Balance, Creditor/Debtor
balances, Purchase/Sales/General ledgers) showed that framing was wrong: the books run **on credit
with payment booked as a separate Payment Voucher / Official Receipt**, so a bill **always** becomes
an AP/AR Invoice and *payment never reroutes it*. The real driver of where a document lands is the
**counterparty relationship and its Creditor/Debtor code**, not payment timing. The Decision below
is the amended position; `payment_status`-as-module-driver is withdrawn.

## Context

Real Malaysian ERPs (AutoCount, SQL Account) post a supplier bill to an **AP/AR Invoice** module
**against a Creditor/Debtor code** — a per-client master record under the AP/AR control account.
They reserve a separate **CashBook** module (Payment Voucher / Official Receipt) for **direct cash
movements with no supplier/customer account** (petty cash, a utility paid directly, a one-off
receipt). A paid bill from a *tracked* supplier is **not** a CashBook entry — it is an AP Invoice
that a later **Payment Voucher settles** (knocks off). Invoice and payment are **two separate
records** linked only by the counterparty code.

Ledgr sits **upstream of the ERP with no live connection** to it, so it cannot query the client's
live creditor/debtor master. But the client already **produces that master as a standard report**
(Creditor Balance / Debtor Balance), and Ledgr already ingests the client's COA the same way
(ADR-0006). **Xero and QBS Ledger have no CashBook split** — a bill posts to the direction's sheet
and payment is reconciled separately.

## Decision

1. **The Accounting Module driver is the counterparty, not payment.** A bill from a tracked
   supplier/customer posts to the target's **AP / AR Invoice** module against that party's
   **[[Creditor / Debtor code]]**. A one-off cash expense with **no** such account posts to a
   **CashBook** module (PV/OR) straight to an expense/income GL account.
2. **The Creditor/Debtor code is per-client master data the client owns — Ledgr never invents it.**
   Ledgr learns a code (Correction / Entity_Memory, or by ingesting the client's own
   Creditor/Debtor balance report) and resolves a document's counterparty to it. Same authority
   rule as the COA (ADR-0006): no generic default, no example client's codes baked in.
3. **Payment is settlement, in the bank lane — not an invoice field.** Whether a bill is paid is
   realised as a separate event arriving through the bank lane (a bank-statement line; in
   AutoCount/SQL a PV/OR that knocks off the invoice). Ledgr does **not** stamp paid/credit on an
   invoice to route it. Auto-matching bank payments to invoices (knock-off automation) is a
   **later, separate** concern, not part of the invoice path.
4. **A target that requires the code can't post a credit document without it.** A brand-new
   counterparty takes **one** Review (HITL) pause to capture its code (the client supplies it per
   their own scheme), after which it is remembered (consistent with ADR-0027). The graceful path:
   an unknown counterparty on a **paid** document falls to CashBook (no code needed), so it never
   blocks.
5. **Profile-driven, no ERP names in code.** Each target's modules, sheets, preview columns and
   Workbook tabs are described by **that target's own YAML profile** (rule-data). The routing
   Python reads the profile ("does this target declare a CashBook block?") and contains no ERP
   names. Export, Excel workbook, Slack preview and Job summary all follow the profile; the
   Completeness Contract gains a declared module's required headers.
6. **Sequencing:** producing **AP/AR Invoices against a resolved code** is the core invoice path
   (universal — every target needs purchase/sales + a counterparty). The **CashBook no-supplier
   path** and any **settlement/knock-off automation** are deferred (Phase B), behind the eval gate.

## Consequences

- The dominant case (a supplier bill from a tracked creditor) books correctly to AP/AR Invoice with
  the client's own code — no phantom, because a real supplier balance is the point.
- The "phantom creditor" problem narrows to its true scope: a one-off **paid** receipt with **no**
  supplier account → CashBook PV/OR, not an AP Invoice that never gets knocked off.
- The "code sync" gap (no live ERP link) is closed with a document the accountant already produces
  (the balance report), not a bespoke integration.
- Until the CashBook path lands, a one-off paid receipt with no code can't post a clean credit
  invoice — it pauses or is handled as a direct expense — a known, bounded interim.
- Adding/altering a module stays a YAML edit; presentation surfaces follow.

## Alternatives considered

- **Module = Direction × Payment status** (the original framing) — mis-models real books: a paid
  bill from a tracked supplier is an AP Invoice settled by a later PV, not a CashBook entry.
  Withdrawn on real-client evidence.
- **Stamp `payment_status` on every invoice as a universal field** — adds a concept that doesn't
  drive routing and invites the LLM to over-reach; payment belongs in the bank lane. Rejected.
- **Ledgr invents/auto-creates Creditor/Debtor codes** — risks duplicate/mismatched master records
  in the client's real ERP; violates "client's scheme is authority" (ADR-0006). Rejected.
- **Require a full creditor/debtor master sync before any processing** — heavier than the
  soft-gate philosophy; balance-report ingest + one-pause-per-new-counterparty degrades more
  gracefully. Rejected as a hard gate (kept as an optional pre-seed).
- **Hardcode CashBook routing in Python keyed on the ERP name** — violates dynamic-by-data;
  rejected.

## Sources

- ADK / Gemini — structured output as the read layer; rule-data in YAML / Skill `assets/` (see
  ADR-0026 sources).
- AutoCount & SQL Account official help / import docs (CashBook vs AP/AR Invoice; creditor/debtor
  mandatory on invoices; CashBook needs no counterparty code) — verified 2026-06-26.
- Real SQL Account client audit pack (reference only, **not** committed) — grounded the
  credit-then-settle workflow and the counterparty-code-as-driver finding.
