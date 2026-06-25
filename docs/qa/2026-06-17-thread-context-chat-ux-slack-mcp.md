# Thread context + chat UX — Slack MCP live QA

Date: 2026-06-17  
Channel: `#skyline-international-pte-ltd` (`C0BASC8U551`)  
Scope: Thread delivery context (Phases 1–3) + agentic chat UX (Phase 4). No Chrome.

## Preconditions

1. Bot restarted with latest `accounting_agents/slack_runner.py` + `assistant.py`.
2. Slack app has `reactions:write` scope (manifest updated; reinstall if needed).
3. `LEDGR_CHAT_UX=1` (default on).

## Manual smoke

| Step | Action | Pass |
|------|--------|------|
| 1 | Batch-upload 2 PDFs (e.g. 25-D15 + 25-D12) | Job summary posts with FY2025 ledger |
| 2 | Reply in thread: `Why account code for 25-D15?` | 👀 on your message within ~1s |
| 3 | Wait for reply | Thinking shimmer visible during run |
| 4 | Read bot reply | Cites 6-3000 / Professional Fees; no vendor prompt |
| 5 | Follow-up: `yes file name 25-D12` | Still scoped to thread batch; FY2025 |

## Slack MCP verification

Use these MCP tools (Cursor Slack plugin):

| Check | Tool | What to verify |
|-------|------|----------------|
| Delivery thread | `slack_read_thread(channel_id=C0BASC8U551, message_ts=<summary_ts>)` | Parent = delivery card; replies include user Q + bot A |
| Eyes ack | same thread read | User question message has 👀 reaction (or ✅ after reply) |
| Bot answer | same thread read | Bot message mentions account code / 6-3000 / 25-D15 |
| Channel search | `slack_search_public_and_private(query="in:#skyline-international-pte-ltd 25-D15")` | Finds delivery + thread replies |

## Regression flags (fail if present in bot reply)

- "provide the vendor"
- "don't see any document"
- "current session is set to FY2026" (when batch was FY2025)
- "please provide" (vendor/description prompt)

## Local probe (no Slack)

```bash
uv run python scripts/ledgr_chat_live_probe.py --case B7_chat_thread_delivery_context_trajectory \
  --question "Why account code for 25-D15 in this delivery?"
```

Expected trajectory: `lookup_row` → `explain_categorization`.
