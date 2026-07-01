# Test cleanup inventory — light path

What we **removed** and what **replaced** the old accounting-agent test stack.

Live testing docs: [testing-process.md](testing-process.md).

---

## Removed test modules (old robot brain)

| Path | Why removed |
|------|-------------|
| `tests/eval/` | Old eval home → `ledgr_agent/eval/` |
| `tests/test_routing.py` | Legacy FY routing |
| `tests/test_import_readiness.py` | Old NormalizedInvoice import notes |
| `tests/test_account_flagged.py` | HITL / COA confidence flags |
| `tests/test_credit_note_sign_exporter_row.py` | → `tests/ledgr_agent/test_erp_projection.py` |
| `tests/test_erp_exporters.py` | NormalizedInvoice multi-ERP exporters (not light path) |
| `tests/test_tax_classifier.py` | TaxClassifier LLM-adjacent rules (printed tax on light path) |
| `tests/test_partial_failure.py` | Old reconcile / page-gap warnings on NormalizedInvoice |
| `tests/unit/test_dummy.py` | Placeholder |

Also removed legacy sections from:

- `tests/test_axis_resolvers.py` — TaxClassifier reference classes
- `tests/test_ledger_doc_identity.py` — exporter-based AutoCount doc-key tests

### Scripts removed

| Path | Why |
|------|-----|
| `scripts/trim_*_phase_a.py` | One-off HITL trimmers |
| `legacy/accounting_agents/adk_web_qa.py` | ADK web QA for removed graph |

### Local-only (gitignored)

| Path | Why |
|------|-----|
| `tests/eval_invoices/*.pdf` | Private PDFs for old eval |

### QA docs — historical only (`docs/qa/archive/`)

Do **not** run; they target HITL, COA upload, and `accounting_agents`.

---

## Live replacements

| Old | New |
|-----|-----|
| `tests/eval/test_eval_golden.py` | `ledgr_agent/eval/test_h_ledgr_light_live.py` |
| Full Slack E2E | `tests/ledgr_agent/test_slack_ledgr_e2e.py` |
| Spine integration | `tests/integration/test_light_path_delivery.py` |
| ERP column checks | `tests/ledgr_agent/test_erp_projection.py` + `tests/test_export_skills.py` |
| Live smoke | [light-path-live-smoke.md](light-path-live-smoke.md) |
| Manual ERP import | [erp-import-matrix.md](erp-import-matrix.md) |

---

## CI commands

```bash
# Default — what CI runs (~660 tests, ~4s parallel)
uv run pytest -n auto

# Full suite including slow bank formulas
uv run pytest

# Integration harness
uv run pytest tests/integration

# Live Gemini eval (needs creds)
./scripts/ledgr_eval_light.sh
```

---

## Still in repo but not on live Slack path

| Path | Notes |
|------|-------|
| `legacy/accounting_agents/` | Archived; import-isolation tests block live imports |
| `legacy/invoice_processing/` | Archived factory |
| `ledgr_slack/export/exporters.py` | NormalizedInvoice exporters — preview/ledger helpers only |

Do **not** add new tests against NormalizedInvoice / TaxClassifier / HITL.
