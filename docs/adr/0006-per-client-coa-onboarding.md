# 0006 — Per-client COA onboarding: three capture paths, soft gate, validate-on-upload

- **Status:** Accepted
- **Date:** 2026-06-13
- **Deciders:** Ledgr team; informed by market research (Dext, AutoEntry, Hubdoc, QBO, Xero).

## Context

ADR-0004/0005 establish that the client's **own** COA is the single source of truth
for categorisation, and the generic "standard SG SME COA" (`standard_coa_rows()` /
`ledgr_use_standard_coa`) is being removed because account code numbers differ per
client — a generic default produces confidently-wrong codes a firm must unwind.

That leaves an onboarding gap: **how do we obtain and validate a client's COA**,
especially when the client hasn't provided one yet? Research into how leading
document-automation tools handle this found a consistent pattern: **import the COA
from the client's accounting software (sync, don't author); learn per-vendor by
confirmation; never hard-block ingestion; never seed a shared generic COA.**

## Decision

**Three capture paths**, offered in-channel when a firm onboards a client:
- **A — Upload:** CSV/XLSX, including a native Xero/QBO COA export as-is. Parse,
  validate (below), echo a parse-back summary, firm confirms.
- **B — Guided export:** bot gives the exact export steps for the client's current
  software (Xero: *Accounting → Advanced → Chart of Accounts → Export*; QBO: *Chart
  of Accounts report → Export*), then funnels the file into Path A.
- **C — Build-it-for/with-them:** extract a draft COA from prior financials / trial
  balance / tax return (or a photo); if nothing exists, offer an **editable starter
  scaffold owned by this client** (never a shared standard), then **round-trip** the
  agreed COA back into the firm's software so codes match Ledgr exactly.

**Soft gate (not a hard block):**
- Documents **always ingest and extract** — even with no COA — and sit in a
  **"pending — needs COA"** state with a clear nudge.
- Lines are **only written to the import template against a validated COA** (you
  cannot assign a code that doesn't exist; junk codes are worse than waiting).
- **No generic default COA** is ever used.

**Validate on upload** (surface failures inline before accepting):
required fields **code · name · type**; codes unique; sane code format (≤10 chars,
consistent scheme); account type in the allowed class enum; tax code (if present) in
the channel's jurisdiction set (SG GST / MY SST) and class-compatible (warn);
coverage sanity (some income **and** some expense accounts); **parse-back
confirmation by the firm = the activation gate.**

**Warm-start calibration:** the first N documents surface every line for
confirmation (AutoEntry-style); each confirmed `vendor → code` / tax becomes a
remembered [Correction](0004-learning-via-structured-corrections-not-memory-bank.md).

## Consequences

- High activation (process now, code against the real COA later) with correctness
  (no confidently-wrong generic codes).
- Integrates with HITL (ADR-0003) and Corrections (ADR-0004): pending/flagged lines
  are reviewed, fixes are remembered.
- The round-trip (Path C → firm's software) prevents code drift between Ledgr and the
  authoritative books.
- `standard_coa_rows()` + `data/standard_sg_sme_coa.json` + the `ledgr_use_standard_coa`
  button are removed (plan Step 1b).

## Alternatives considered

- **Hard-block until COA provided** — kills onboarding activation; unnecessary since
  extraction is independent of coding; rejected.
- **Generic default-then-remap** — produces confidently-wrong codes; the exact
  failure mode that motivated removing the standard COA; rejected.

## Sources
Dext (COA import on connect; process-without-software), AutoEntry (per-supplier/
line-item/tax rules from confirmations), QBO & Xero (custom COA import at setup,
account components & validation, merge-on-duplicate), bookkeeping-onboarding practice
(build COA from prior financials). URLs captured in the research thread.
