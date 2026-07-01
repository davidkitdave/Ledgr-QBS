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

- Lint: `uv run ruff check app ledgr_agent ledgr_slack tests`
- Tests: `uv run pytest` runs the fast hermetic suite (**785** tests; **868** collected with slow/legacy excluded). Live Gemini eval: `ledgr_agent/eval/` (opt-in). Integration: `tests/integration/`.
- Run app (dev): `uv run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload`.
- Socket Mode (dev): `uv run python -m ledgr_slack` (needs `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN`).

Non-obvious gotchas:

- Live runtime is **two packages only**: `ledgr_slack` (Slack + ledger) and `ledgr_agent` (AI read + sheets).
- `app.main:app` is the live HTTP entrypoint (`build_fastapi_app` from `ledgr_slack`).
- `/healthz` returns HTTP 503 with `{"missing": ["SLACK_BOT_TOKEN", ...]}` until
  Slack creds are set — this is expected, not a failure. `/openapi.json` returns 200.
- `POST /slack/events` 500s without GCP Application Default Credentials: the lazy
  handler builds a Firestore session service via `google.auth.default()`. Full
  Slack/Firestore handling needs ADC + Slack tokens (`SLACK_BOT_TOKEN`,
  `SLACK_SIGNING_SECRET`/`SLACK_APP_TOKEN`) and `GOOGLE_API_KEY` (AI Studio) for
  Gemini. None are present by default, so the unit suite runs offline with deterministic stubs.

### Live Slack test from the VM (no Cloud Run, no real GCP)

A full human→bot→Slack roundtrip is reproducible locally:

1. Run a Firestore emulator (Java is present): download
   `cloud-firestore-emulator-*.jar` and `java -jar … --host=127.0.0.1 --port=8090`,
   then `export FIRESTORE_EMULATOR_HOST=127.0.0.1:8090` so `firestore.Client()`
   uses it without GCP credentials.
2. Seed a per-channel client profile into the emulator via
   `FirestoreClientStore.save_profile/set_channel` (or use `/ledgr settings` in Slack).
   Set `status="active"` with accounting software and FYE month — no COA upload required.
3. Start Socket Mode: `uv run python -m ledgr_slack` (needs
   `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN`). It prints "⚡️ Bolt app is running!".
4. The dev Slack app (`ledgr-dev` in workspace `QBS-AI`) is in **Socket Mode**, so
   the running instance is the workspace's sole event consumer. A plain channel
   message triggers the document pipeline (`read_doc` → `build_sheets`). The bot lacks `channels:join` scope — invite it at channel
   creation instead of self-join.
5. **Caveat:** while a Socket-Mode instance runs here, it intercepts events for the
   whole workspace (including real client channels) using only the local emulator
   state — stop it after testing so it doesn't shadow production usage.
