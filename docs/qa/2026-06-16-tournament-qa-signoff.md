# Extraction tournament QA (2026-06-16)

## Round 1 — Sample Test Group (complete)

**Winner: V3** (enhanced normalizer + segmentation prompt)

| Variant | Avg score | False splits |
|---------|-----------|--------------|
| V3 | 0.40 | 0 |
| V2 | 0.21 | 1 |
| V0 | 0.16 | 1 |
| V1 | 0.11 | 1 |

Key fix: expense claim went from **6 documents → 1** under V3.

Reports:
- [tournament_round1_report.json](tournament_round1_report.json)
- [tournament_round1_rubric.md](tournament_round1_rubric.md)
- [tournament_winner_baseline.json](tournament_winner_baseline.json)

## Production wiring (T3)

- Phase 1 prompt includes expense-package segmentation rules
- Enhanced normalizer (`mapper_version=enhanced`) default
- Package merge safety net in `normalize_document_node`
- Pipeline uses `normalize_path_two_phase` when `extract_fn=extract_file`

## Round 2 — SOA/telco (scaffold)

```bash
.venv/bin/python -m eval.extraction_tournament_soa --hermetic
LEDGR_SOA_PDF=/path/to/soa.pdf .venv/bin/python -m eval.extraction_tournament_soa
```

## Live eval

```bash
.venv/bin/python -m eval.document_record_eval --live --baseline
.venv/bin/python -m eval.extraction_tournament --variants V0,V1,V2,V3
```

## agents-cli regression

After tournament runs, compare baselines:

```bash
agents-cli eval compare docs/qa/tournament_winner_baseline.json docs/qa/tournament_round1_report.json
```

Python helper: `eval/tournament_metric.py`

## Phase 2 ledger policy (2026-06-16)

- **Foreign single-currency invoices** (USD for SGD client): booked in document currency; no FX flag.
- **Mixed-currency same document**: flag only when payout cannot be resolved to one currency.
- **Expense claims**: employee as supplier; USD payout line(s) only when receipts are in another currency.
- **Dates**: `15 Jan 2025`, ordinals, and date ranges parse into ledger `invoice_date`.
- **SOA packages**: cover/summary table dropped in Phase 2 (`_is_soa_phantom_record`); embedded invoices kept.

## SOA live test (Sample Vendor Inc)

```bash
# Hermetic (always runs in CI)
uv run pytest tests/test_soa_document_record.py -q

# Live PDF (requires LEDGR_TEST_DOC_DIR + Gemini)
LEDGR_TEST_DOC_DIR=~/Desktop/LocalTest/TestDoc \
  uv run pytest tests/test_soa_document_record.py::test_cool_power_two_phase_live -v

# Or demo script
uv run python scripts/local_soa_demo.py
# LEDGR_SOA_PDF=/path/to/soa.pdf uv run python scripts/local_soa_demo.py
```

Ground truth: **10 invoices, 22 lines**, page 1 skipped, no phantom IA-073xx numbers.

## QA status

| Gate | Status |
|------|--------|
| A Anti-hard-code | PASS (single prompt, generic schema) |
| B Live PDF fidelity | PASS — `scripts/local_extract_demo.py` + post-fix tournament |
| C Pipeline two-phase | PASS (`pipeline_spine.py` + merge in normalizer) |
| D Slack smoke | Pending human |
| P1-7 struggle signals | PASS (false split, normalize_incomplete) |
| P1-8 ledger policy | PASS (FX + dates + reimbursement) |
| P1-9 QBS export path | PASS — `scripts/local_ledger_preview.py` |
| P1-10 SG Xero tax | PASS — SR/ZR/No Tax + non-reg GST absorb (`scripts/local_tax_verify.py`) |

## Post-fix tournament (2026-06-16)

After FX/date/claim/SOA fixes, **V3 scores 1.0** on all four Sample Test Group fixtures:

```bash
uv run python -m eval.extraction_tournament --variants V3 \
  --output docs/qa/tournament_post_fix_report.json
```

Report: [tournament_post_fix_report.json](tournament_post_fix_report.json)  
Baseline updated: [tournament_winner_baseline.json](tournament_winner_baseline.json)

## Full QBS export preview

Phase 1 → 2 → tax → categorize → `QbsLedgerExporter` + header completeness:

```bash
uv run python scripts/local_ledger_preview.py > docs/qa/local_ledger_preview_report.json
```

Sample Test Group: 4 PDFs, 100% QBS header fill, COA assigned.  
Sample Vendor Inc SOA: 10 invoices / 22 QBS rows, `MYR` currency, dates filled, no phantoms.

## SG Xero tax codes + non-reg absorption

**GST-registered** client: telco/freight lines export `*TaxType` **SR** or **ZR** with tax split.  
**Not GST-registered** client: every line **No Tax**; input GST **absorbed** into line amount (no separate tax column).

```bash
uv run pytest tests/test_ws2_ws3.py::TestNonRegisteredGstAbsorption -q
uv run python scripts/local_tax_verify.py   # live PDFs under ~/Desktop/LocalTest/TestDoc
```

Xero mapping in `invoice_processing/shared_libraries/sg_gst.yaml`: `SR`, `ZR`, `No Tax`.

## ledger_eval on Purchase PDFs

Discovery prefers `**/Purchase/**/*.pdf`. Explicit fixtures:

```bash
uv run python -m eval.ledger_eval --paths \
  ~/Desktop/LocalTest/TestDoc/Sample\ Test\ Group/Company-A/Purchase/FY2026/INV-2026-003-Vendor Alpha\ Paid.pdf \
  ~/Desktop/LocalTest/TestDoc/MYDoc/Sample\ Auto\ Enterprise/Purchase/SOA-SAMPLE-DEC-2025_.pdf
```
