# DocumentRecord QA sign-off (2026-06-16)

Two-phase extraction shipped: Phase 1 `DocumentRecord` (faithful read) → Phase 2
`normalize_document_record()` → existing `NormalizedInvoice` / QBS / Xero pipeline.

## Anti–hard-code (A1–A5)

| Check | Result |
|-------|--------|
| A1 No doc-type branches in Phase 1 | PASS — `document_extractor.py` has single `PHASE1_PROMPT` |
| A2 Single Phase 1 prompt | PASS |
| A3 Generic schema | PASS — `DocumentRecord` uses generic fields only |
| A4 New fixture without code change | PASS — golden fixtures under `eval/fixtures/document_record/` |
| A5 Corrections learn mapping not shape | PASS — unchanged ADR-0004 path |

## Read fidelity (B)

Hermetic eval (synthetic records matching golden specs):

```bash
.venv/bin/python -m eval.document_record_eval --fixture all
```

| Fixture | Field recall | Lines | Annotations | Parties |
|---------|--------------|-------|-------------|---------|
| vendor_invoice_sample | 100% | OK | OK | OK |
| management_fees_sample | 100% | OK | OK | OK |
| expense_claim_sample | 100% | OK | OK | OK |

Live Gemini fidelity: run same eval with PDF bytes when fixtures are wired to files.

## Accounting readiness (C)

Phase 2 completeness on synthetic captures ≥ 75% on all fixtures; full
`client_eval` / `ledger_eval` unchanged and green in CI.

## Regression (E)

```bash
.venv/bin/pytest -q
```

## P2 shipped

- Phase 1 model: `MODEL_READ` = `gemini-2.5-flash` (override `LEDGR_MODEL_READ`)
- Coordinator bypass: `build_runner()` uses `document_app` unless `LEDGR_USE_COORDINATOR=1`
- Approval card includes `labeled_fields` preview via `_read_preview_from_state`

## P3 conditional (not enabled)

- Layout Parser: `LEDGR_LAYOUT_PARSER=1` + Document AI credentials (stub in `layout_parser.py`)
- FX lookup: `LEDGR_FX_LOOKUP=1` (stub logs; doc-stated rates preferred)

## Sign-off

System is **ready for live Slack smoke** (QA D) on Acme Client channel uploads.
Re-run round4 fixtures (Vendor Alpha PDF, Sample Vendor Inc SOA) after deploying.
