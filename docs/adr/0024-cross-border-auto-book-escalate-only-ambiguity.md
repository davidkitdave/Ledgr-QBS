# ADR-0024: Auto-book routine cross-border purchases; escalate only genuine ambiguity

Status: Accepted (2026-06-19)
Builds on ADR-0011 (intelligent understand layer), ADR-0017 (quality-gated HITL).
Relates to the jurisdiction workstream in the IDP→ERP plan (WS2 / WS2b).

## Context

A live adk-web QA pass (`docs/qa/2026-06-19-adk-web-qa-findings.md`) found that **every
cross-border document** — a foreign-supplier invoice for a local client — unconditionally
stopped for HITL. `resolve_jurisdiction` hardcoded `flag_for_human=True` on both
cross-border branches, and `tax_reasoning._reason_one_invoice` then forced every line to
`tax_flagged=True` with the reason *"(no jurisdiction rule); HITL review required"*. A
Malaysian client receiving a Singapore TelcoTwo bill (which shows explicit 9% SG GST) was
flagged on both lines and blocked.

We grounded the correct behaviour in primary tax guidance (IRAS e-Tax Guides for SG GST;
RMCD MySST guides for MY SST — see the QA doc's research brief). The finding: a
foreign-supplier purchase is **routine bookkeeping**, not a judgement call. The foreign tax
is simply part of the cost; the transaction is **out of scope** for the local GST/SST return
(not claimable as input tax). An estimated 85%+ of cross-border supplier invoices for typical
commercial clients follow this deterministic path. Only a few patterns genuinely need a human.

### The profile question (clarified)

A client's **home tax jurisdiction (SG vs MY) is a fixed entity-level fact** — an
SG-incorporated company files GST with IRAS; an MY one files SST with RMCD. It does not vary
per document. It is already stored as `region` in the client profile. That is the *right*,
non-rigid anchor: set once at onboarding, never re-derived. The *intelligent* part is
per-document — read the supplier country off the invoice and reason about it against that
stable home jurisdiction. **No new "which country is this client" field is needed.** The only
profile addition is an optional `partial_exempt` flag (default False), used solely to catch
the one SG case that genuinely needs reverse-charge review (a partially-exempt financial-sector
client).

## Decision

Make cross-border handling **intelligent, not rigid**:

- **Auto-book the routine case.** Cross-border → `tax_system=OS` and `flag_for_human=False` by
  default. `tax_reasoning` books every line as `OS` (out of scope), confidence 0.9, not
  flagged, with a calm reason: *"Foreign-counterparty purchase; out of scope for local
  GST/SST; foreign tax recorded as shown, not claimable as input tax."* Deterministic — no LLM
  call (it is not a judgement). The document flows through approval (auto-approved) to delivery.
- **Escalate only genuine ambiguity.** `flag_for_human=True` is reserved for:
  - a **partially-exempt SG client** (`partial_exempt=True`) — imported-services reverse charge
    needs review (implemented now);
  - **AMBIGUOUS** jurisdiction — client region unknown or region/currency mismatch (unchanged).
  - Future triggers (noted, not yet wired): ambiguous MY First-Schedule service category,
    intercompany/related-party invoices, mixed goods+services, unknown supplier country.
- **`region` stays the stable home-jurisdiction anchor.** Per-document we reason only about
  supplier/customer country and supply nature.
- **Persist the flag through state.** `resolve_jurisdiction` now writes `flag_for_human` and
  `cross_border` into session state, and `resolution_from_state` reads them back, instead of
  re-deriving `flag_for_human` from the jurisdiction code. This fixes a parallel-derivation bug
  whereby the flag could never actually be turned off for a cross-border code.

## Consequences

- Routine cross-border purchases (overseas SaaS, telco, vendors) auto-book and deliver — the
  manual bottleneck the QA flagged is gone. Live-verified: the MY-client TelcoTwo bill went from
  a HITL stop (2 flagged lines) to `CROSS_BORDER/OS`, `flagged_lines=0`, `auto_approved`,
  delivered.
- A partially-exempt SG client still gets the reverse-charge review (set `partial_exempt=True`
  in their profile). Default False, so the common case is auto.
- Booking cross-border as `OS` is conservative and correct for purchases (foreign tax = cost,
  not claimable). A future refinement could distinguish cross-border **sales** (exports →
  zero-rated) from purchases, and add the MY service-category classifier for the ITS 8%/6%
  self-accounting path — both are improvements on top of "don't block", not regressions.
- Domestic SG/MY behaviour is byte-identical; AMBIGUOUS still escalates. Suite 1864 passed / 6
  pre-existing failures.
