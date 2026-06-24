# Clean Agent Cutover - Live QA Checklist

## Purpose

This checklist gates the flip of `LEDGR_USE_CLEAN_AGENT` from off (default) to on
in dev. It is the manual verification step for the clean ADK accountant agent
rebuild (Plans 1-6). All seven checks must pass against the dev Slack workspace
before the flag default is changed. The production flip is a separate operator
action and is explicitly out of scope here.

References:

- Plan: `docs/superpowers/plans/2026-06-24-clean-adk-accountant-agent-implementation.md`
- Spec: `docs/superpowers/specs/2026-06-24-clean-adk-accountant-agent-design.md`

## Preconditions

Before running any check:

- `LEDGR_USE_CLEAN_AGENT=1` is set in the dev workspace environment.
- `uv run pytest tests/ledgr_agent -q` is green locally.
- Dev bot (socket mode) and `adk web` have been restarted from HEAD (a stale
  long-running instance will silently run old code and make fixes look broken).
- A registered-client SG channel is available, and a separate channel per ERP
  target (AutoCount, SQL Account) is available. Invoice numbers below are
  synthetic (INV-001..INV-007) so they do not collide with real client data.

## Manual Checks

### 1. Normal Invoice - No HITL

Goal: A clean, high-confidence registered-client SG invoice goes straight to
delivery without a human-in-the-loop card.

Steps:

1. Drop an SG tax invoice into the registered-client channel. Use synthetic
   invoice number INV-001.
2. Wait for the bot's status message and any review card.

Expected outcome:

- No review card is posted.
- A delivery card appears with a Block Kit ledger table and the per-document
  xlsx attachment.
- `validation_summary.hitl_required` is `false` in the trace.

Pass criteria:

- Delivery card posted within the normal latency budget.
- `tests/ledgr_agent` still green (no regression in auto-approve path).

### 2. Grouped COA Review

Goal: Low COA confidence on multiple lines produces a SINGLE grouped soft
warning, not one bullet per line.

Steps:

1. Drop an invoice whose line items have low COA confidence across more than
   one line. Use synthetic invoice number INV-002.
2. Watch the review card the bot posts.

Expected outcome:

- Exactly one grouped soft warning card, with a "Lines affected" list.
- Card severity is `review` (soft), not `hard_review` (hard stop).

Pass criteria:

- Card count is 1, not equal to the number of low-confidence lines.
- The grouping message names the affected lines and the single underlying
  reason (for example, "5 lines flagged for low COA confidence").

### 3. Approve Grouped Review

Goal: Pressing Approve on the grouped review card delivers the document.

Steps:

1. Reuse the INV-002 card from check 2.
2. Click the Approve button on the grouped review card.

Expected outcome:

- Delivery card appears with the same ledger content as a non-HITL flow.
- The grouped review card is marked approved (thread state updated).
- No re-extraction occurs; the original review state is committed.

Pass criteria:

- Single delivery card, single ledger write, ledger pointer updated.

### 4. Edit Row With Confirmation

Goal: Chat amend action asks for confirmation before mutating the ledger.

Steps:

1. In the chat agent for the same channel, send a message that names the row
   to amend on INV-001 (for example: "Change the tax code on row 2 of INV-001
   to ZR").
2. Observe the tool call the agent makes.

Expected outcome:

- The amend tool surfaces a confirmation prompt before mutating.
- Ledger is NOT mutated until the user confirms.

Pass criteria:

- The tool call is gated by ADK tool confirmation.
- After the user confirms, the row is updated and the chat replies with the
  diff.

### 5. AutoCount / SQL Export

Goal: The exporter selected by the channel profile produces the right column
layout for AutoCount and for SQL Account.

Steps:

1. Drop an invoice into the AutoCount-client channel. Use synthetic invoice
   number INV-003.
2. Drop an invoice into the SQL Account-client channel. Use synthetic invoice
   number INV-004.
3. Inspect the xlsx attachments.

Expected outcome:

- INV-003 xlsx has the AutoCount import columns (per golden file in
  `localtest/erp_templates/autocount/`).
- INV-004 xlsx has the SQL Account import columns (per golden file in
  `localtest/erp_templates/sql_account/`).

Pass criteria:

- Column headers and ordering match the golden file for the respective ERP.
- Amounts and tax codes are recorded as-shown on the source invoice (no FX
  conversion).

### 6. Zero-Credit Block

Goal: A firm with zero balance is blocked before any LLM call is made.

Steps:

1. Set the firm's credit balance to 0 in the profile.
2. Drop an invoice into the channel. Use synthetic invoice number INV-005.
3. Inspect the trace and the bot's reply.

Expected outcome:

- Bot replies with a "blocked" message that names the reason (zero credit).
- No LLM call is recorded in the trace for this document.
- `validation_summary.block_reason` is `zero_credit`.
- `BatchResult.status` is `blocked`.

Pass criteria:

- Trace shows zero LLM calls for INV-005 after profile read.
- No ledger write, no exporter invocation, no partial delivery.

### 7. Dedup No Charge

Goal: A duplicate submission does not double-charge the firm.

Steps:

1. Re-submit a previously-charged batch (same document set). Use synthetic
   invoice number INV-006 for the new file plus REC-001 as the duplicate
   receipt.
2. Inspect the trace and the bot's reply.

Expected outcome:

- The duplicate document is marked `credit_status=not_billable`.
- The credit ledger is NOT debited a second time.
- `validation_summary.block_reason` is `duplicate` for the duplicate doc.

Pass criteria:

- Single credit debit for the original batch only.
- Duplicate doc surfaces a clear "already processed" message to the user.

## Retirement Candidates

This task does not delete code. The table below classifies the retirement
candidates; actual deletion is a follow-up gated on this checklist passing.

| Path | Classification | Action after green QA |
|------|----------------|------------------------|
| `accounting_agents/agent.py` document graph | legacy-reference | Deprecate; keep until flag default flips |
| `accounting_agents/nodes.py` review paths | live | Shrink after shared review module proven |
| `eval/` scripts | legacy-reference | Delete when pytest/agents-cli parity exists |
| `legacy/` | safe-to-remove | Delete after import scan |

Before any deletion, run an import scan:

```bash
uv run python -c "import ast, pathlib; roots=['legacy']; print('scan ok')"
uv run pytest -q
```

## Default Flip Protocol

Change `LEDGR_USE_CLEAN_AGENT` default to on in dev manifest only. Production
flip is a separate operator action, gated on this checklist passing.

## Sign-Off

QA lead: ___
Date: ___
Workspace: ___
