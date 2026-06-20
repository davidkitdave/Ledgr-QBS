---
name: write-gated
description: |
  Gated ledger writes — amend, remove, clear month, re-extract, learn mapping.
metadata:
  author: ledgr-qbs
  version: "1.0"
---

# Skill: write-gated

Use when the user wants to fix, delete, clear, re-read, or teach a mapping rule.

## Before any amend/remove

1. Call ``lookup_row`` to get the exact ``row_index`` — never guess.
2. Propose via ``amend_ledger_row`` or ``remove_ledger_row`` (confirmation gated).
3. User must reply **yes** before anything is written.

## Clear a month

``replace_recorded_month`` — gated; purges dedupe keys so docs can be re-dropped.

## Re-extract

``re_extract_document`` needs ``file_id`` (from ``list_recent_documents``) and a
non-empty ``hints`` string. Gated — goes through normal Approve card.

## Learn immediately

``learn_mapping`` has **no** confirmation — call as soon as user says "remember X → Y".

## Restrictions

- Invoice rows (Purchase/Sales) only — bank rows are read-only.
- Tax is re-derived by the engine (non-GST-registered → NT).
