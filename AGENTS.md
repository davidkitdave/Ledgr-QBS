# Ledgr-QBS

## Agent skills

### Issue tracker

Issues live in GitHub Issues for your-org/Ledgr-QBS. See `docs/agents/issue-tracker.md`.

### Triage labels

Default label vocabulary (needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: CONTEXT.md + docs/adr/ at the repo root. See `docs/agents/domain.md`.

## Cursor Cloud specific instructions

Python project managed with `uv` (Python 3.12 in the VM; `requires-python >=3.10`).
The update script runs `uv sync`, so deps (incl. the `dev` group: ruff, pytest) are
already installed at session start. Standard commands:

- Lint: `uv run ruff check .` — the repo currently has pre-existing lint errors
  (mostly in `tests/`); a non-zero exit is the baseline, not an environment break.
- Tests: `uv run pytest` runs the fast unit suite (~680 tests, all green). The
  `tests/integration` and `tests/eval` suites are excluded by default (see
  `addopts` in `pyproject.toml`) because they make live Gemini calls and boot a
  server; run them deliberately only with real creds.
- Run app (dev): `uv run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload`.

Non-obvious gotchas:

- `app.main:app` is the live entrypoint (`build_fastapi_app` from
  `accounting_agents/slack_runner.py`). The `README.md` "Running the Agent" section
  is stale (it describes the retired `adk web invoice_processing` flow).
- `/healthz` returns HTTP 503 with `{"missing": ["SLACK_BOT_TOKEN", ...]}` until
  Slack creds are set — this is expected, not a failure. `/openapi.json` returns 200.
- `POST /slack/events` 500s without GCP Application Default Credentials: the lazy
  handler builds a Firestore session service via `google.auth.default()`. Full
  Slack/Firestore handling needs ADC + Slack tokens (`SLACK_BOT_TOKEN`,
  `SLACK_SIGNING_SECRET`/`SLACK_APP_TOKEN`) and `GOOGLE_API_KEY` (AI Studio) for
  Gemini. None are present by default, so only the deterministic engine and the
  unit suite run offline.
- `invoice_processing/pipeline.py` is the hermetic engine/eval harness, NOT the
  live runtime. It injects every LLM step as a keyword-only callable, so it (and
  the unit tests) run end-to-end with deterministic stubs and zero network/LLM
  calls — the right way to demo the core classify→extract→categorize→tax→route→
  workbook flow without creds.
