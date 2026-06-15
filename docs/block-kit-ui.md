# Block Kit UI

Native Slack primitives that replace the old plain-text + section-button UX across the document pipeline.

## Quick reference table

| Block | Surface | When it fires | User action |
|-------|---------|---------------|-------------|
| plan | channel | every document drop | watch progress; no action needed |
| card (per-doc) | channel | after a doc lands cleanly | optional Re-extract / Edit / View row |
| card (dedup callout) | channel | duplicate month detected | Replace recorded month / Keep existing |
| data_table (ledger preview) | channel | after delivery | scroll, filter, sort; download .xlsx for full export |
| context_actions (feedback) | channel | under each clean per-doc card | 👍 confirms mapping; 👎 reopens for re-extract |
| card (approval gate) | channel | end of pipeline, needs human OK | Approve / Edit / Reject |
| card (review gate) | channel | mid-pipeline, extraction looks off | Re-extract with hint / Looks right keep it / Reject |
| card (proactive redo) | channel | after delivery if reviewer flagged | Re-extract / Skip |

---

## Plan block — pipeline step-tracker

**What it is:** a collapsible Slack "thinking" panel showing 5 pipeline stages (Classifying → Extracting → Categorizing → Applying tax → Awaiting approval) with status icons for each (✓ complete, ✨ in-progress, ⏳ pending).

**Replaces:** a single line of text that was being chat_update-edited as the pipeline advanced — the user couldn't tell which stage was running or how far along it was.

**When it fires:** immediately after a document is uploaded to a channel where Ledgr is invited. Updated live as each stage completes.

**User benefit:** at a glance, the user sees which pipeline stage is running right now. If the bot stalls, the sparkle (in-progress marker) shows which step stopped. Each stage can optionally show output (e.g. "Vendor: Acme · Total: SGD 1,234.50") when it finishes, giving real-time transparency into what the bot found.

**Action:** none — read-only; the plan block auto-advances as the run proceeds.

**Wiring:** `app/blocks.py:processing_plan_blocks()` — called from `accounting_agents/slack_runner.py:_update_status()` whenever the ADK event loop reports a stage change.

**Fallback:** if the workspace doesn't support native `plan` blocks, emits a classic section + context block with emoji markers instead.

---

## Per-doc card — summary with inline actions

**What it is:** a Slack card (title / subtitle / body layout) summarizing one processed document: vendor/bank name, invoice number and date, total and currency, tax code, account code, and FY destination.

**Replaces:** a one-line mrkdwn section that was truncated at 2900 characters and had no inline action buttons.

**When it fires:** after each document in a batch is processed cleanly (reconciled = True, no errors).

**User benefit:** readable at a glance; the invoice/bank details are visually grouped instead of comma-separated. The 3 inline buttons let the user Re-extract, Edit, or View the row in the ledger without hunting for a separate approval card.

**Actions:** 
- **Re-extract** — re-read the same file with a hint (opens the re-extract modal).
- **Edit** — jump to the line-edit modal for this document (same as approval-gate Edit).
- **View row** — open the ledger workbook scrolled to this document's row (if supported by the viewer).

**Wiring:** `app/blocks.py:per_doc_card()` → `_per_doc_card_native()` for the card shape, or `_per_doc_card_fallback()` for the legacy section+actions. Called from `app/blocks.py:result_card()` after delivery.

**Fallback:** if the workspace doesn't support native `card` blocks, reverts to the original mrkdwn section + actions block layout.

---

## Dedup callout card — "I already have this month"

**What it is:** a warning card (⚠️ icon, yellow accent) that fires when the dedup guard detects that you're uploading invoices for a vendor in a month that's already been recorded.

**When it fires:** mid-pipeline, before the document is filed. Shows side-by-side stats: what's already recorded vs. what you just uploaded.

**User benefit:** transparent choice — you can see the existing row count and date range, then decide whether to replace the prior month's data or keep it.

**Actions:**
- **Replace recorded month** — throws away the old data and files the new batch instead (shortcut to `replace_recorded_month` chat tool).
- **Keep existing** — ignores the new upload and keeps what's already in the ledger.

**Wiring:** `app/blocks.py:dedup_callout_card()` → called from the dedup guard in the pipeline when a duplicate is detected.

**Fallback:** if the workspace doesn't support native `card` blocks, emits a `section` + `actions` block with the same text and buttons.

---

## Data table — ledger preview after delivery

**What it is:** a native Slack table (Date · Description · Account · Tax · Net · Total) showing the rows just appended to the FY ledger. Built-in filtering, sorting, and pagination (10 rows/page, up to 100 rows per table).

**Replaces:** a fixed-width mrkdwn code block that was hard to read and read-only.

**When it fires:** immediately after successful delivery, right below the per-doc cards. Shows the last ~10 rows posted (or fewer if the batch was small).

**User benefit:** verify the filing at a glance without downloading the workbook. Filter by account or vendor, sort by date, scroll through the appended rows. If the batch had >100 rows, a context line notes "+N more in the workbook above" and the .xlsx file is still the full record.

**Actions:** sort, filter, and paginate within the table itself — no buttons needed.

**Wiring:** `app/blocks.py:ledger_preview_data_table()` → called from `accounting_agents/slack_runner.py:persist_and_deliver()` after ledger rows are persisted.

**Fallback:** if the workspace doesn't support native `data_table` blocks, shows the classic fixed-width mrkdwn preview instead.

---

## Context actions — feedback buttons (👍 / 👎)

**What it is:** a pair of feedback buttons (thumbs up / thumbs down) under each successfully processed per-doc card.

**When it fires:** under every clean per-doc card in the delivery summary — but only after the document has been filed (no action pending from the user).

**User benefit:** quick signal that feeds two systems: 👍 routes to the `learn_mapping` chat tool (teaches the bot the vendor → account → tax code triple for future autonomy); 👎 routes to the re-extract modal pre-populated with "user flagged" (gives the bot a chance to re-read with that context).

**Actions:**
- **👍** (positive) — confirms the mapping; the bot logs the vendor / account / tax code triple and gets smarter on future invoices from the same vendor.
- **👎** (negative) — re-extract with the hint that something looked off; opens the same modal as the per-doc card's Re-extract button.

**Wiring:** `app/blocks.py:feedback_buttons_block()` → emits `context_actions` + `feedback_buttons` primitives. Called from `app/blocks.py:result_card()` under each filed per-doc card.

**Fallback:** if the workspace doesn't support native `context_actions`, emits two small buttons in a classic `actions` block instead.

---

## Approval gate card — "Review needed before adding to the ledger"

**What it is:** a card (🔍 icon, 3 buttons) that fires when the pipeline escalates a document to human review because extraction or reconciliation hit a struggle signal (unmatched totals, unclear doc type, missing fields, etc.).

**When it fires:** end of the extract + categorize + tax stages, before approval. **The pipeline pauses here** — the document is not yet in the ledger.

**User benefit:** explicit decision point with context. The card shows what flagged the doc (e.g. "Totals didn't match the summary") and up to 3 buttons to move forward.

**Actions:**
- **Approve** — file the document as extracted (trust the bot).
- **Edit** — open the line-edit modal to correct account codes, tax treatments, or amounts before filing.
- **Reject** — discard the document entirely (it won't appear in the ledger).

**Wiring:** `app/blocks.py:approval_card_blocks()` → emits a native `card` with title / body / actions. Called from `accounting_agents/slack_runner.py:_post_approval_interrupt()` when a `RequestInput` escalation arrives.

**Fallback:** if the workspace doesn't support native `card` blocks, shows a `section` + `actions` block with the same layout and buttons.

---

## Review gate card — "Extraction needs your input"

**What it is:** a card (🔍 icon, 3 buttons) that fires mid-pipeline, after extraction, when the extractor's confidence or line quality is flagged for review.

**When it fires:** between extract and categorize stages. **The pipeline pauses here** — the document is not yet categorized or in the ledger.

**User benefit:** transparent escalation with named struggle signals. The card shows the extractor's precise question (e.g. "This looks like a bundle of receipts — which account should they go to?") and a bulleted list of why it was flagged (e.g. "Multiple documents detected").

**Actions:**
- **Re-extract with a hint** — opens a modal so you can describe what the extractor missed (e.g. "This is a tax invoice, not a receipt"). The bot re-reads with your hint and tries again.
- **Looks right, keep it** — waves the current extraction through unchanged (the bot did fine, move on).
- **Reject this doc** — discard the document entirely.

**Wiring:** `app/blocks.py:review_card_blocks()` → emits a native `card` + optional `context` blocks for the question and struggle signals. Called from `accounting_agents/slack_runner.py:_post_review_interrupt()` when a `:review` interrupt arrives.

**Fallback:** if the workspace doesn't support native `card` blocks, shows a `section` + `actions` block with the same layout and buttons.

---

## Proactive redo card — "Want to re-extract this with a hint?"

**What it is:** a card that fires **after** a document has already been filed, as a graceful offer to re-read it if the reviewer flagged it during or after delivery.

**When it fires:** post-delivery. Shows up under a per-doc card if the review stage detected something odd but didn't pause the pipeline (the document was already approved/auto-filed, and we want to give you a chance to fix it without blocking).

**User benefit:** non-blocking feedback loop. The bot says "I filed this, but something looked a little off — want me to re-read it?" and names what looked off (e.g. "unreconciled totals"). You can click the button to re-extract with a hint, or ignore it and move on.

**Actions:**
- **Re-extract with a hint** — opens the same hint-input modal as the review gate, pre-loaded for the filed document.

**Wiring:** `app/blocks.py:proactive_redo_blocks()` → emits a native `card` with an optional single button + explanatory `context` block. Called from `accounting_agents/slack_runner.py:persist_and_deliver()` if post-delivery review flags are set.

**Fallback:** if the workspace doesn't support native `card` blocks, shows a `section` + `actions` block with the same layout and button.

---

## Feature flag — LEDGR_NATIVE_BLOCKS

Controls which block builders are emitted:

- **`auto`** (default) — the new native blocks are emitted; automatically falls back to section+actions if a workspace doesn't render them (uses a per-channel cache).
- **`1` / `true` / `yes`** — force native blocks globally (useful for testing).
- **`0` / `false` / `no`** — force the legacy section+actions shape (useful for clients on older Slack desktop clients that don't render the new blocks).

The cache is populated by `app.native_blocks_compat.record_probe_result(channel_id, supported)` — called by the smoke script or a manual probe utility. Once cached, `supports_native_blocks(channel_id)` returns the stored result for that channel.

Set the flag via environment variable:
```bash
export LEDGR_NATIVE_BLOCKS=auto  # default
export LEDGR_NATIVE_BLOCKS=1     # force native
export LEDGR_NATIVE_BLOCKS=0     # force fallback
```

---

## Known follow-ups (not in this PR)

- **Plan block stays in-progress after Approve / Reject** — After a human approves or rejects a document at the approval gate, the plan block's "Awaiting approval" stage doesn't mark as complete. The next batch will move past it cleanly, but a single document approval leaves the stage stuck on ✨. See task #10.

- **Plan block per-stage `output` rich_text not populated in live runs** — The plan block's per-task `output` field (e.g. extract output showing vendor + total) is built correctly but isn't filled in during live pipeline runs. Works in unit tests. See task #11.

- **Bot says "Approved" but doesn't upload the FY workbook for unreconciled docs** — Pre-existing pipeline bug, unrelated to this PR. After approval of a flagged document, the ledger rows are persisted but the .xlsx workbook isn't re-uploaded to the channel. See task #12.

---

See the [planning doc](file:///Users/davidkitdave/.claude/plans/i-just-notice-that-mellow-lollipop.md) for the design history and architectural decisions.
