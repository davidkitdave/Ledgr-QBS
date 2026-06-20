# Ledgr-QBS

## Agent skills

### Issue tracker

Issues live in GitHub Issues for davidkitdave/Ledgr-QBS. See `docs/agents/issue-tracker.md`.

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
- **Gemini backend selection (most important gotcha):**
  `invoice_processing/shared_libraries/genai_client.make_client()` defaults to
  **Vertex AI** when `GOOGLE_GENAI_USE_VERTEXAI` is unset, so live engine calls
  fail with a "default credentials were not found" (ADC) error. For dev, create a
  `.env` (gitignored) with `GOOGLE_GENAI_USE_VERTEXAI=FALSE` so it uses AI Studio
  via `GOOGLE_API_KEY` (the documented `cp .env.example .env` step). The FastAPI
  app and unit tests already get this because importing `accounting_agents.config`
  sets the flag FALSE, but standalone engine modules do not — they need the `.env`.
- `/healthz` returns 200 `{"ok":true}` only when `SLACK_BOT_TOKEN` and
  `SLACK_SIGNING_SECRET` are set; otherwise HTTP 503 with the missing list
  (expected, not a failure). `/openapi.json` returns 200 regardless.
- `POST /slack/events` 500s without GCP Application Default Credentials even with
  Slack tokens present: the lazy handler builds a Firestore session service via
  `google.auth.default()`. The full Slack message roundtrip therefore needs GCP
  ADC (service-account JSON via `GOOGLE_APPLICATION_CREDENTIALS`, or
  `gcloud auth application-default login`) in addition to the Slack tokens.
- `invoice_processing/pipeline.py` is the hermetic engine/eval harness, NOT the
  live runtime. It injects every LLM step as a keyword-only callable, so it runs
  the full classify→extract→categorize→tax→route→workbook flow either live (real
  Gemini, with the `.env` above) or with deterministic stubs (unit tests, no creds).

### Live Slack test from the VM (no Cloud Run, no real GCP)

A full human→bot→Slack roundtrip is reproducible locally:

1. Run a Firestore emulator (Java is present): download
   `cloud-firestore-emulator-*.jar` and `java -jar … --host=127.0.0.1 --port=8090`,
   then `export FIRESTORE_EMULATOR_HOST=127.0.0.1:8090` so `firestore.Client()`
   uses it without GCP credentials.
2. Seed a per-channel client profile + COA into the emulator via
   `FirestoreClientStore.save_profile/set_channel/save_coa` (the modal-driven
   `/ledgr settings` onboarding can't be automated). Set `status="active"` and use
   `app/data/standard_sg_sme_coa.json` for the COA.
3. Start Socket Mode: `python -m accounting_agents.slack_runner` (needs
   `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN`). It prints "⚡️ Bolt app is running!".
4. The dev Slack app (`ledgr-dev` in workspace `QBS-AI`) is in **Socket Mode**, so
   the running instance is the workspace's sole event consumer. A plain channel
   message triggers the Q&A agent (`answer_question`); a file upload triggers the
   document pipeline. The bot lacks `channels:join` scope — invite it at channel
   creation instead of self-join.
5. **Caveat:** while a Socket-Mode instance runs here, it intercepts events for the
   whole workspace (including real client channels) using only the local emulator
   state — stop it after testing so it doesn't shadow production usage.
