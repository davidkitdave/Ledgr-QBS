---
name: ledger-read
description: |
  Read-only ledger questions — bank vs invoice tool selection, month filters,
  and empty-ledger diagnostics.
metadata:
  author: ledgr-qbs
  version: "1.0"
---

# Skill: ledger-read

Use when the user asks about balances, P&L, spend by category, GST threshold,
recent activity, or row lookup.

## Bank vs invoice

- **Bank statement** loaded (rows have Withdrawal/Deposit/Balance) → ``bank_totals``
  with optional ``month`` / ``year``.
- **Invoice ledger** (Purchase/Sales sheets, Source Amount, COA) →
  ``summarize_by_category``, ``pnl_for_fy``, ``gst_threshold_check``.

## Empty or wrong FY

If tools report no rows, call ``diagnose_assistant_context`` first and cite
``fy_pointers`` before telling the user to upload.

## Never

Do not invent numbers. Do not call write tools for read-only questions.
