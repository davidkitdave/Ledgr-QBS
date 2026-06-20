# adk web QA findings — 2026-06-19

Live pass on a freshly-restarted `adk web` (from HEAD, `LEDGR_FIRESTORE_NAMESPACE=dev`
so the run wrote to isolated `dev_*` collections). Probe document: a real **StarHub
Singapore telco bill** (`scratch/qa_docs/` — gitignored), run once as a Malaysian client
and once as a Singapore client.

## What works well (verified end-to-end)

- **Full 12-event pipeline runs in adk web** on current code: classify → extract → review
  → categorize → resolve_jurisdiction → tax → approval_gate → apply → route → consolidate
  → deliver.
- **SR/ZR split** — the telco bill was correctly extracted into two lines (Standard-Rated
  9% + Zero-Rated 0%).
- **Domestic SG is smart and frictionless** — SG client: `tax_jurisdiction=SINGAPORE`,
  `tax_system=GST`, `flagged_lines=0`, `decision=auto_approved`, delivered
  *"📒 Added 2 lines from 1 document to … Ledger FY2025 (QBS Ledger)."*
- **Playground seed holds on fresh code** — the SINGAPORE default profile seeds at
  `classify_node` (the 38e9691 fix is intact).
- **HITL escalation UI is excellent** — on a flagged doc the run pauses at `approval_gate`
  with a precise, line-by-line message and a Form/JSON approve/edit/reject control.
- **Graceful no-PDF failure** — sending a message with no file yields a clear, actionable
  `ValueError` ("upload a PDF or image file with your message") instead of a crash.

## Finding 1 — cross-border purchases have no rule and ALWAYS escalate (real edge) — ✅ RESOLVED (ADR-0024, commit 59b85d3)

> **Resolved 2026-06-19.** Cross-border purchases now auto-book as out-of-scope (foreign tax
> recorded as shown, not claimable), escalating only genuine ambiguity (partially-exempt SG
> client, AMBIGUOUS jurisdiction). Live re-verified: the same MY-client StarHub bill went from
> a HITL stop (2 flagged lines) to `CROSS_BORDER/OS`, `flagged_lines=0`, `auto_approved`,
> delivered. See ADR-0024. Original finding below for the record.


Same StarHub bill, **Malaysian** client: `tax_jurisdiction=CROSS_BORDER`,
`tax_system=OS` (out of scope), **both lines flagged**, HITL stop with reason
*"CROSS_BORDER: jurisdiction=CROSS_BORDER (no jurisdiction rule); HITL review required"*.

The document itself shows explicit **"GST @ 9%"** (Singapore GST), i.e. the tax is already
determined on the supplier side. Marking it "out of scope / needs a human" is technically
defensible for a foreign-supplier purchase (SG GST is not claimable MY input tax), but the
**"(no jurisdiction rule)"** reason shows the real cause: there is **no handling rule for
the foreign-supplier → local-client case, so every such document unconditionally stops for
human review.** Any firm that regularly receives overseas-vendor invoices (SaaS, telco,
freight…) would hit a manual gate on each one.

**Recommendation (feeds the plan's jurisdiction workstream / WS2b):** add a cross-border
*purchase* rule that records the foreign tax **as shown** and books it out-of-scope for the
local GST/SST return **without** mandatory HITL — escalate only when genuinely ambiguous
(e.g. unknown supplier country), and replace "(no jurisdiction rule)" with a specific,
calm explanation. This is the "less rigid, more intelligent" direction.

## Finding 2 — `playground_profile.json` silently overrides the SINGAPORE default (footgun)

`seed_playground_profile_if_needed` defaults to SINGAPORE, but if a `playground_profile.json`
exists at the repo root it wins. One was present (`region=MALAYSIA`, a real-looking company
name), so **every** adk web run silently became a Malaysian client — which is what produced
the cross-border result above and could be mistaken for a regression. The file is now
gitignored (it may carry real client data). Consider: log the seeded region at startup, or
require `LEDGR_PLAYGROUND_PROFILE_PATH` to opt into a non-default profile.

## Not yet run

- `multi receipt.pdf` (20 MB, multi-entity split) — the next best edge-probe; deferred for
  time. Worth a dedicated run to check multi-receipt-per-page splitting.
