# 0005 — Software-agnostic canonical schema, projected per target, with a completeness contract

- **Status:** Accepted
- **Date:** 2026-06-13
- **Deciders:** David (developer)

## Context

Extracted documents must land in the client's accounting-software **import
template** — QBS Ledger and Xero — for both sales and purchase. Those templates
have **different headers**: QBS Ledger has no tax-code column (tax is implied by the
amount), while Xero requires `*`-marked columns (`*InvoiceDate`, `*UnitAmount`,
`*AccountCode`, `*TaxType`, …).

A natural-sounding idea was raised: make the **extraction schema itself BE the
accounting-software headers**. The risk: a schema shaped like one software's headers
cannot cleanly serve the other, and re-extracting per target is wasteful and
divergent.

The repo already implements the better pattern: a single software-agnostic model
(`NormalizedInvoice` / `BankStatement`) plus per-software **exporters**
(`QbsLedgerExporter`, `XeroLedgerExporter`) that project that model into each
template at write time. The real concern behind the question is legitimate, though:
extraction must fill **every field each target template requires**, or cells come
out blank.

## Decision

Keep **one software-agnostic canonical schema** (`NormalizedInvoice` /
`BankStatement`) as a **superset** of what any target needs. Map it to each target
with **per-software exporters** (one extraction → many exports).

Add an explicit **completeness contract**: the set of fields extraction must fill is
the **union of every target template's REQUIRED headers** (Xero's `*` columns + QBS
Ledger's required set, for both sales and purchase). Extraction quality is measured
and enforced as **per-required-header fill rate, per target** (the eval in the build
plan, steps 0 and 7), not as a single aggregate score.

## Consequences

- One extraction serves QBS and Xero (and any future target) without re-work.
- "Extract based on the accounting software" is honoured **as a completeness
  contract derived from the templates**, while keeping the clean canonical+projection
  shape.
- Adding a new target = a new exporter + folding its required headers into the
  contract; the canonical schema and extractor change only if the new target needs a
  field no existing target did.
- The eval becomes header-aware: a missing `*UnitAmount` for Xero or a blank
  `Total Amount` for QBS is a measurable, attributable failure.

## Alternatives considered

- **Canonical schema = one software's headers** — couldn't serve the other target
  cleanly; rejected.
- **Separate extraction per target** — duplicated, divergent, wasteful; rejected.

## Addendum (2026-06-14) — target resolution, no silent default

Live testing surfaced a failure of *this* ADR in practice: a client onboarded as
**Xero** had its ledger written with the **QBS** template. Root cause (confirmed):
the chosen target (`accounting_software`) reached the exporter only via the
coordinator's `before_agent_callback`, whose state write did **not** propagate to
the export node (`consolidate_node`), which then fell back to a silent
`state.get("software") or "qbs"` default. The profile in Firestore was correct
(`accounting_software = "Xero"`); the target was simply lost in transit and the
silent default masked it.

To make per-target projection reliable:

- The export target is the client's profile `accounting_software`, **loaded into
  run state at run start** (seeded alongside `channel_id` in the runner), not via a
  best-effort callback whose delta may not reach the export node.
- **No silent default.** An unresolved or blank target **blocks the write** (soft
  gate, per ADR-0006 — extract always, write only against a confirmed profile)
  rather than guessing a template.
- Every delivery **echoes the target used** ("Added to your *Xero* FY2026 ledger"),
  so a wrong target is self-evident instead of invisible.
