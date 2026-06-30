# ADR-0035: Bookable row granularity and metadata-first extraction

**Status:** Accepted  
**Date:** 2026-06-30  
**Related:** ADR-0034 (schema-as-prompt), ADR-0033 (reference-free eval), ADR-0011 (Understand layer), ADR-0026 (LLM reads, Python applies)

## Context

Production `read_doc` over-extracted hierarchical bills (many appendix rows in Excel) while itemized invoices and SOA+invoice packs need different grain:

- **Summary bills** — few bookable rows (charge summary or tax buckets).
- **Itemized invoices** — every printed product/service row.
- **SOA packs** — skip debtor statement cover when full invoices follow; one `documents[]` entry per invoice.

Metadata (vendor, dates, totals) belongs in document header fields and Slack text, not as extra Excel rows.

Research via Google Developer Knowledge MCP and ADK docs (2026-06-30) plus internal skeptic review confirmed: **field descriptions are the primary prompt**; Python line repair is an anti-pattern.

## Google research summary

| Finding | Source |
|---------|--------|
| Structured output: header scalars + `lines[]` array | [Gemini structured output](https://ai.google.dev/gemini-api/docs/structured-output) |
| `Field(description=...)` acts as the field-level prompt | Same; ADR-0034 |
| System instruction = role + high-level task only | [Prompting strategies](https://ai.google.dev/gemini-api/docs/prompting-strategies) |
| PDF native vision; steer summary vs detail via schema | [Document processing](https://ai.google.dev/gemini-api/docs/document-processing) |
| Validate semantics in application / eval — not silent repair | Structured output validation guidance |
| Composite PDFs: split logical documents then parse | [Procurement Doc AI](https://cloud.google.com/solutions/procurement-doc-ai) → `documents[]` |

## ADK research summary

| Finding | Source |
|---------|--------|
| Custom metrics grade tool JSON | [ADK custom metrics](https://adk.dev/evaluate/custom_metrics/index.md) |
| `GEPARootAgentPromptOptimizer` rewrites **instruction only** | [ADK optimize](https://adk.dev/optimize/index.md) |
| Improvement loop: eval fixtures → schema descriptions → eval → GEPA (optional) | `ledgr_agent/eval/optimize/run_optimize.py` |

## Decision — two layers, no hardcoding

| Layer | Location | Allowed |
|-------|----------|---------|
| Short instruction | `READ_PROMPT`, `BUNDLE_READER_INSTRUCTION` in `schemas.py` | Role, read-only, reconcile totals; **one** generic structure-conditional anti-detail sentence (charge summary + appendix → summary rows only) |
| Extraction grain | Pydantic `Field(description=...)` on `Line`, `ReadDocument`, `ReadDocumentBundle` | Summary vs itemized via `line_grain` enum + `lines[]` descriptions; SOA split, tax buckets |
| Validation | `ledgr_agent/eval/ledgr_light_metrics.py` | `max_bookable_lines`, `min_bookable_lines`, `expected_document_count`, `forbid_document_kinds` |
| Metadata display | `build_sheets` `documents_summary` + `compose_delivery_summary` | Header fields in Slack; `lines[]` in Excel |
| **Forbidden** | — | Per-doc-type Python branches, vendor names in schema, silent `lines[]` replacement |

### Three document shapes (one schema)

| Shape | `lines[]` grain | Metadata |
|-------|-----------------|----------|
| Hierarchy / summary bill | Summary rows or tax buckets reconciling to subtotal | Header fields → Slack |
| Itemized invoice / credit note | Every printed product row | Header fields → Slack |
| SOA + attached invoices | Itemized per invoice; omit statement page from `documents[]` | Per-document headers → Slack |

### Optional notes-only annotation

`ledgr_agent/internal/extraction_notes.py` may set `notes` when line count suggests over-extraction on `line_grain=summary` docs. It **never** mutates `lines[]`.

### `line_grain` commitment (2026-06-30)

Schema-as-prompt alone was insufficient for `flash-lite` on a real 18-page telco bill (204 appendix rows vs 3 in ADR-0030 spike). Add `line_grain: Literal["itemized", "summary"]` on `ReadDocument` with `propertyOrdering` so the model commits before filling `lines[]`. Default `itemized` protects normal invoices. One generic anti-detail sentence in `BUNDLE_READER_INSTRUCTION` is permitted (structure-conditional, not per vendor/type).

| `line_grain` | When | `lines[]` |
|--------------|------|-----------|
| `itemized` | Product table, no charge-summary section | Every printed charge row |
| `summary` | Charge/tax summary + appendix/detail pages | Summary rows reconciling to subtotal only |

### Tax-bucket sub-mode under `summary` (2026-06-30)

When a summary bill prints **more than one GST treatment** (e.g. Standard-Rated 9% + Zero-Rated 0% on Singapore telco bills), `lines[]` should be **one row per tax bucket**, not service-category rows (Internet/Mobile/Switch). Amounts come from the printed tax summary; `tax_breakdown` must be filled.

**Description convention (Xero import style):** for telecommunications bills use `Telephone charges (SR)` and `Telephone charges (ZR)` in the Excel Description column; **Tax Treatment** carries the full printed label (`Standard-Rated 9%`, `Zero-Rated 0%`). See [`docs/research/sg-gst-tax-codes.md`](../research/sg-gst-tax-codes.md) §3.3.

When only **one** tax treatment is printed (e.g. single GST 9% telco summary), keep charge-category summary rows — not tax buckets.

| Case tag | Metric |
|----------|--------|
| `expect_tax_buckets: true` | `extraction_tax_bucket_fidelity` |
| `expect_hierarchy_scope: true` (no tax buckets) | `extraction_bookable_granularity` |

## Eval extension

| Metric | Purpose |
|--------|---------|
| `extraction_bookable_granularity` | `max_bookable_lines` on hierarchy cases |
| `extraction_itemized_fidelity` | `min_bookable_lines` on itemized cases |
| `extraction_tax_bucket_fidelity` | SR+ZR tax buckets + Xero-style descriptions on tagged cases |
| `extraction_classification` | `forbid_document_kinds` catches SOA leaking into splits |

Synthetic fixtures: `sg_gst_invoice_multiline`, `soa_plus_multiline_invoices`, `soa_plus_credit_note`, `telco_sr_zr_summary`.

## Anti-patterns (do not reintroduce)

1. Per-document-type branches in `read_doc.py` (telco regex, vendor names).
2. Long `BUNDLE_READER_INSTRUCTION` with enumerated doc types.
3. Collapsing itemized invoices to tax buckets in Python.
4. Post-extract silent line replacement as the primary fix.
5. Expecting GEPA alone to fix grain (it cannot edit schema descriptions).

## Consequences

- Schema field edits are the main quality lever; regenerate eval datasets after fixture changes.
- Slack delivery shows Drive-style document headers; Excel remains one row per bookable line.
- See also CONTEXT.md [[Bookable row granularity]] and [[Schema-as-prompt extraction]].

## Skeptic review

2026-06-30 skeptic subagent scored the pre-revision plan 6/10; this ADR incorporates fixes: eval-first, schema-only grain, notes-only safety net, Slack headers in scope.
