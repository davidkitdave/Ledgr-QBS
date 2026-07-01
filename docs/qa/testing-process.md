# Testing process — light path

How we test **`ledgr_slack` + `ledgr_agent`** after the accounting-agent removal (ADR-0032).

Historical runbooks for the old stack live in [`archive/`](archive/) — **do not run those checklists**.

---

## Two layers

| Layer | What | When |
|-------|------|------|
| **Automated** | `uv run pytest`, optional live eval | Every PR; weekly eval cron |
| **Hands-on** | Socket Mode smoke, ERP import clicks | Weekly + before prod deploy |

**Rule:** computers check rules (math, FY, column shapes); humans check feel (Slack cards, real PDFs, ERP import).

---

## Every pull request

```bash
uv run ruff check app ledgr_agent ledgr_slack tests
uv run pytest -m "not slow"
```

CI runs the same gate (see [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)).

---

## When `ledgr_agent/` changes

```bash
# Needs GOOGLE_API_KEY or GOOGLE_CLOUD_PROJECT
./scripts/ledgr_eval_light.sh
```

Or pytest eval:

```bash
uv run pytest ledgr_agent/eval/test_h_ledgr_light_live.py -m eval -v
```

Weekly cron: [`.github/workflows/eval.yml`](../../.github/workflows/eval.yml).

After prompt/model changes, compare regressions:

```bash
./scripts/ledgr_eval_compare.py   # if baseline artifacts exist
```

---

## Weekly human smoke

1. Kill stale Socket Mode instances.
2. Run [light-path-live-smoke.md](light-path-live-smoke.md) section **10** (minimum 9 steps).
3. Tick one row in [erp-import-matrix.md](erp-import-matrix.md) per ERP you touched.

---

## Before production deploy

1. Full [light-path-live-smoke.md](light-path-live-smoke.md) sections 0–9.
2. Credit billing spot check (drop doc → balance decreases → footer updates).
3. Confirm `LEDGR_FIRESTORE_NAMESPACE` and model env point at prod.

---

## Adding a feature

1. One **hermetic** pytest (mock Gemini / fake Firestore).
2. One line in [light-path-live-smoke.md](light-path-live-smoke.md) if user-visible.
3. If extraction logic changes, add or extend a case in `ledgr_agent/eval/`.

---

## Live path spine (what tests should target)

```
Slack upload → process_file_event → read_doc → build_sheets → deliver_workbook → append_rows
```

Do **not** write new tests against `process_document_batch`, HITL interrupts, or COA categorizer — those are removed.

---

## Quick reference

| What | Command |
|------|---------|
| Unit suite | `uv run pytest` |
| Fast suite (skip slow ledger formulas) | `uv run pytest -m "not slow"` |
| Parallel local | `uv run pytest -n auto` |
| Integration harness | `uv run pytest tests/integration` |
| Live eval | `scripts/ledgr_eval_light.sh` |
| Socket Mode dev | `uv run python -m ledgr_slack` |
| Health | `GET /healthz` on FastAPI app |
