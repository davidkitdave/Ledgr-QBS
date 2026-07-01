# Test cleanup inventory — light path

What we **removed**, what is **legacy but kept**, and what **replaced** the old accounting-agent test stack.

Live testing docs: [testing-process.md](testing-process.md).

---

## Removed (safe to delete — already gone or gitignored)

### Test modules (legacy stack)

| Path | Why removed |
|------|-------------|
| `tests/eval/` (entire tree) | Old two-lane eval home; replaced by `ledgr_agent/eval/` + `tests/test_eval_routing.py` |
| `tests/test_routing.py` | Legacy `export/routing.py` archive path; FY now in `ledgr_agent/internal/fy.py` |
| `tests/test_import_readiness.py` | Pre-import notes for old NormalizedInvoice exporters |
| `tests/test_account_flagged.py` | HITL / low-confidence COA flags on removed path |
| `tests/test_credit_note_sign_exporter_row.py` | Covered by `tests/ledgr_agent/test_erp_projection.py` |
| `tests/unit/test_dummy.py` | Scaffold placeholder (`assert 1 == 1`) |

### Scripts

| Path | Why removed |
|------|-------------|
| `scripts/trim_test_app_blocks_phase_a.py` | One-off HITL block-kit trimmer |
| `scripts/trim_blocks_phase_a.py` | Same |
| `legacy/accounting_agents/adk_web_qa.py` | ADK web QA for removed graph |

### Local-only fixtures (gitignored — delete on disk anytime)

| Path | Why |
|------|-----|
| `tests/eval_invoices/*.pdf` | Private invoice PDFs for old eval; not referenced by current tests |

### QA docs (historical only — in `docs/qa/archive/`)

Do **not** run these checklists; they target HITL, COA upload, and `accounting_agents`:

- `2026-06-14-live-smoke-qa-checklist.md`
- `2026-06-15-*` ultraqa / accounting-agent plans
- `2026-06-16-*` batch QA / round4 relive
- `testing-strategy.md`, `adk-web-testing.md`, `adk-architecture-map.md`
- `credit-system-live-qa-checklist.md` (partially stale)

Optional delete (one-off artifact, not a runbook):

- `docs/qa/tournament_round2_soa_report.json`

---

## Kept but marked `@pytest.mark.legacy` (excluded from default CI)

| Path | Why kept |
|------|----------|
| `tests/test_tax_classifier.py` (~65 tests) | YAML tax reference rules; not on live Slack hot path (printed tax from `read_doc`) |

Run explicitly: `uv run pytest -m legacy`

**Candidate for future legacy mark** (still in default CI today):

- ~~`tests/test_erp_exporters.py`~~ — marked `legacy` (NormalizedInvoice exporters)
- ~~`tests/test_axis_resolvers.py`~~ — `TestResolveTaxClassifierReference` + `TestSalesIndeterminateFlagged` marked `legacy`
- ~~`tests/test_ledger_doc_identity.py`~~ — exporter-based doc-key tests marked `legacy`; core `ledger_doc_identity()` tests stay in CI

---

## Live replacements (use these)

| Old | New |
|-----|-----|
| `tests/eval/test_eval_golden.py` | `ledgr_agent/eval/test_h_ledgr_light_live.py` |
| `tests/eval/eval_routing.py` | `tests/test_eval_routing.py` |
| `tests/eval/custom_metrics.py` | `ledgr_agent/eval/ledgr_light_metrics.py` + `tests/ledgr_agent/test_ledgr_light_metrics.py` |
| Full Slack E2E (mocked LLM) | `tests/ledgr_agent/test_slack_ledgr_e2e.py` |
| Spine integration | `tests/integration/test_light_path_delivery.py` |
| Live smoke checklist | [light-path-live-smoke.md](light-path-live-smoke.md) |
| Manual ERP import | [erp-import-matrix.md](erp-import-matrix.md) |

---

## High-leverage testing changes (in progress)

| Lever | Change |
|-------|--------|
| **Faster CI** | `addopts = "-m 'not slow and not legacy'"`; CI uses `pytest -n auto` (~753 tests, ~5s) |
| **Slow tests** | Bank formula edge cases in `test_ledger_store.py` auto-marked `slow` |
| **Trim deps** | Eval-only deps in `google-adk[eval]`; `xlrd` for legacy golden reads only |
| **CI split** | `ci.yml` = ruff + hermetic pytest; `eval.yml` = live Gemini weekly |
| **Automated eval** | `scripts/ledgr_eval_light.sh` + 16 cases in `ledgr_agent/eval/` |
| **Hands-on** | Weekly [light-path-live-smoke.md](light-path-live-smoke.md) + ERP matrix |

### Commands

```bash
# Default (fast) — what CI runs
uv run pytest -n auto

# Full suite including slow bank formulas + legacy tax classifier
uv run pytest

# Integration harness only
uv run pytest tests/integration

# Live Gemini eval (needs creds)
./scripts/ledgr_eval_light.sh
```

---

## Still in repo but not on live path (do not add tests here)

| Path | Notes |
|------|-------|
| `legacy/accounting_agents/` | Archived graph; import-isolation tests block live imports |
| `legacy/invoice_processing/` | Archived factory |
| `docs/superpowers/archive/` | Historical plans — leave as archive |

---

## Next cleanup candidates (not done yet)

1. ~~Mark `test_erp_exporters.py` as `legacy`~~ — done
2. Delete `docs/qa/tournament_round2_soa_report.json` after confirming no one references it — done
3. Update `CONTEXT.md` eval paths (`tests/eval/...` → `ledgr_agent/eval/...`) — done in prior commit
4. Consider marking `test_partial_failure.py` if light path never uses NormalizedInvoice reconcile flags
