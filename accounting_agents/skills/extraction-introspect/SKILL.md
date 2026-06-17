---
name: extraction-introspect
description: |
  Extraction and SOA introspection — how a document was processed, which
  pipeline path ran, and what is waiting for review.
metadata:
  author: ledgr-qbs
  version: "1.0"
---

# Skill: extraction-introspect

Use when the user asks how a file was extracted, whether SOA parsing was
correct, what came in recently, or what needs approval.

## Tool chain

1. ``diagnose_assistant_context`` — snapshot FY, row count, log depth, pending reviews.
2. ``list_processing_history`` or ``list_recent_documents`` — pick the file.
3. ``get_document_processing_detail`` — merge delivery log + doc session snapshot.
4. ``list_pending_reviews`` — HITL interrupts for this channel.

## Explain path

For "why this categorization/tax" on a **ledger row**, use ``explain_categorization``
/ ``explain_tax_treatment`` / ``explain_document_processing`` instead.
