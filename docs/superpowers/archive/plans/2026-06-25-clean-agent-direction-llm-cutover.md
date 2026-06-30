# Plan: Move clean-agent direction (purchase/sales) from difflib to the LLM

**Date:** 2026-06-25
**Branch:** feat/ledgr-my-sst-correctness
**Type:** TDD + eval-gated behavior change (direction → affects tax dispatch + routing)
**Execution:** Sonnet executor; Opus verify pass. Eval-gated before cutover is final.

## Problem

The user asked: "purchase vs sales — shouldn't the LLM identify that via a prompt instead of code?"
Yes — and a live trace (2026-06-25) found the LLM mechanism **already exists and is the
production decider on the legacy Slack graph**, but the clean agent (the future prod agent
per ADR-0026) discards it and uses brittle `difflib` fuzzy matching instead.

### What the trace established (file:line)

- **Slack graph (legacy):** `classify_node` deliberately sets `direction="auto"`
  (`accounting_agents/nodes.py:588`) so the extraction LLM's `direction_for_client`
  field decides, via `_effective_direction` (`ledger_extract.py:601`). Client identity
  (name + UEN) IS injected into the extraction prompt
  (`_build_faithful_extract_dynamic_prompt`, `ledger_extract.py:421`). There is even a
  second-LLM retry when the first returns `unknown` (`nodes._retry_resolve_direction_llm`).
  **`resolve_direction` (difflib) is never called here.**

- **Clean agent (future prod):** `document_engine.py:119-123` calls `resolve_direction`
  (difflib, `document_classifier.py:244`), then `document_engine.py:124` hardens it:
  `effective_direction = direction if direction in ("purchase","sales") else "purchase"`,
  and passes it as a concrete `direction=` string into `process_invoice_document`.
  Because `_effective_direction` only honors the LLM when `direction=="auto"`,
  **the LLM's `direction_for_client` is computed and then silently discarded**, and a
  difflib miss **silently defaults to "purchase" with no HITL flag** — a latent
  mis-booking bug (a sales doc difflib can't match is booked as a purchase).

- **Consumers of the final direction:** `NormalizedInvoice.doc_type` (`"sales"|"purchase"`,
  set in `invoice_extractor.to_normalized:501`) drives both
  `routing.route_document` (Sales vs Purchase sheet, `routing.py:54`) and
  `tax_classifier.classify_line` dispatch between `_classify_sales`/`_classify_purchase`
  (`tax_classifier.py:330`).

- **Client identity is available** in state for the clean agent
  (`client.client_name`, `client.client_uen` on the `ClientContext` passed in).

## Goal / Non-goals

**Goal:** Make the clean agent's purchase/sales direction decided by the extraction LLM's
`direction_for_client` (client-identity-aware, already live on Slack), with `unknown`/ambiguous
routed to HITL instead of silently defaulting to "purchase".

**Non-goals (explicitly OUT of scope):**
- Tax-code logic stays deterministic. `_classify_purchase`/`_classify_sales` and the master
  gate (`if not inv.our_gst_registered → NT`) are unchanged. LLM never decides tax codes.
- The `tax_classifier` keyword-ladder → YAML alias-table refactor is a SEPARATE plan.
- No change to the Slack graph (already correct); this only aligns the clean agent to it.

## Design

**Chosen approach (Option 2 — align clean agent to the proven Slack path):**
In the clean agent invoice lane, stop using `resolve_direction`'s output as the direction.
Pass `direction="auto"` into `process_invoice_document` so the extraction LLM's
`direction_for_client` decides — exactly as `classify_node` already does. Keep the
`classify_file` call (still needed for `doc_type`: bank vs invoice lane routing); only the
*direction* stops coming from difflib.

Why Option 2 over Option 1 (keep difflib primary, LLM only on "unknown"): the user wants the
LLM to identify direction; the LLM path is already production-proven on Slack and is
client-identity-aware with a retry; difflib is the brittle part we're removing. Option 1
would keep difflib as primary — backwards from the intent.

**HITL on uncertainty (correctness fix):** when effective direction resolves to
`unknown`/ambiguous, flag for review using the existing
`invoice_extractor.direction_needs_review` + `append_direction_review_note` helpers, rather
than `document_engine.py:124`'s silent `else "purchase"`. Confirm the clean agent surfaces
that flag to the HITL/pending-reviews surface.

**Seam:** keep `direction_fn`/`classify_fn` as injectable test seams in `document_engine`,
but the live default for the invoice lane passes `direction="auto"`. After cutover,
`resolve_direction` is no longer on the live path (retain for now as a tested helper; a
follow-up may delete it once eval confirms — do NOT delete in this change).

## TDD steps (write failing tests first)

1. **Test: clean agent honors LLM direction.** In `tests/ledgr_agent/` (or the document_engine
   test module), feed a stubbed extraction whose `direction_for_client="sales"` while the
   stubbed classifier issuer/bill-to would make difflib say "purchase". Assert the resulting
   `NormalizedInvoice.doc_type == "sales"`. (Fails today — difflib wins.)
2. **Test: unknown direction flags HITL, not silent purchase.** Stub extraction
   `direction_for_client="unknown"` and difflib unable to match. Assert the doc is flagged
   `direction_needs_review` (and NOT silently booked "purchase"). (Fails today.)
3. **Test: doc_type routing still works.** Confirm classify still yields bank vs invoice lane
   correctly (regression guard that we didn't break `doc_type` by touching the classify call).
4. Make all three pass with the `direction="auto"` change in `document_engine._process_one_path`
   + the HITL-flag wiring.

## Eval gate (must pass before cutover is considered done)

- Use the golden manifest already in the tree: `tests/eval/datasets/golden_manifest.json` +
  `ledgr_agent/metrics/golden_field_match.py` (`tests/ledgr_agent/test_golden_field_match.py`).
  Record the **direction/`doc_type` field-match score on the golden set BEFORE and AFTER**.
  Acceptance: LLM-direction score ≥ difflib baseline (no regression on direction; ideally
  catches sales docs difflib missed). If any golden doc regresses, investigate before finalizing.
- Run the full suite (`uv run pytest tests/ -q`); no new failures vs the 2143-pass baseline
  (1 pre-existing unrelated failure allowed).
- `ruff check` clean on touched files.

## Risk / rollback

- **Low risk:** the LLM path is already the production decider on Slack; this makes the clean
  agent consistent with proven behavior, and is net *less* code on the hot path.
- **Main risk:** LLM direction differs from difflib on some docs → different tax/routing. This
  is exactly what the golden eval gate checks. The Slack production history is corroborating
  evidence it behaves well.
- **Rollback:** single-commit, easily revertible; the `direction_fn` seam still exists so a
  revert is a one-line flip back to passing the resolved string.

## Execution order

1. Opus (me) — this plan. ✅
2. Sonnet executor — write the 3 failing tests, make the `document_engine` change + HITL wiring,
   run suite + golden eval, report before/after scores.
3. Opus verify pass — independent: confirm tests assert the right behavior, eval shows no
   regression, no silent-purchase path remains, Slack graph untouched.
4. User commits when satisfied.
