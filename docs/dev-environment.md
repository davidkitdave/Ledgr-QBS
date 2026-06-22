# Dev environment setup

How to run the local socket-mode bot against your LEDGR-DEV workspace without affecting real customers on the Cloud Run production deployment.

## Architecture

Two completely separate Slack apps share one codebase but have different tokens, different Firestore namespaces, and the same model env vars (`LEDGR_MODEL_*` in `.env` — resolved by `invoice_processing/shared_libraries/model_config.py`):

- **Ledgr (dev)** — local socket-mode process, `LEDGR_ENV=dev`, AI Studio key, `gemini-2.5-flash-lite` for invoices. Installed in LEDGR-DEV workspace only for testing.
- **Ledgr (prod)** — Cloud Run HTTP mode, `LEDGR_ENV=prod`, Vertex AI (asia-southeast1), `gemini-2.5-flash` for both tiers (flash-lite is unavailable in Asia Vertex). Installed in customer workspaces via OAuth.

The two apps share zero state: different bot tokens, different Firestore namespaces (or ideally separate GCP projects), different model backends.

### What actually runs

| Mode | Command | Transport |
|------|---------|-----------|
| **Socket (dev)** | `uv run python slack_bot.py` | Slack Socket Mode → `slack_runner._main_async()` |
| **HTTP (prod mirror)** | `uv run uvicorn app.main:app --host 0.0.0.0 --port 8080` | `POST /slack/events` → same graph |

Both paths build the same ADK graph via `accounting_agents/slack_runner.py`. Socket mode strips OAuth env vars so Bolt uses the bot token directly; HTTP mode keeps OAuth for multi-workspace installs.

The **engine harness** (`invoice_processing/pipeline.py`) is separate: it runs classify → extract → export without Slack or Firestore. Use it (and `uv run pytest`) to iterate on extraction logic without creds.

## Steps

### 1. Create the "Ledgr (dev)" Slack app

1. Go to https://api.slack.com/apps → **Create New App** → **From a manifest**
2. Pick **LEDGR-DEV** as the workspace
3. Paste the contents of `slack/manifest-dev.json`
4. Install to LEDGR-DEV
5. Copy the Bot User OAuth Token (`xoxb-…`) → `SLACK_BOT_TOKEN`
6. Under **Basic Information** → **App-Level Tokens**, generate a token with `connections:write` scope (`xapp-…`) → `SLACK_APP_TOKEN`
7. Copy the Signing Secret → `SLACK_SIGNING_SECRET`

### 2. Configure `.env`

```sh
cp .env.example .env
```

Fill in the three Slack token vars from step 1, your Google AI Studio key (`GOOGLE_API_KEY`), and your dev GCP project id. For Firestore isolation choose one of:

- **Separate GCP project (recommended):** point `GOOGLE_CLOUD_PROJECT` at a dev-only project. Leave `LEDGR_FIRESTORE_NAMESPACE` unset.
- **Shared GCP project:** set `LEDGR_FIRESTORE_NAMESPACE=dev` — collections become `dev_clients`, `dev_channels`, etc. so dev writes never collide with prod (ADR-0022).

**Required in shared-project dev:** always set `LEDGR_FIRESTORE_NAMESPACE=dev`. Without it, QA runs write into prod collections.

### 3. Run the bot

```sh
uv run python slack_bot.py
```

The process connects to LEDGR-DEV via socket mode. Status messages in Slack will be prefixed with `[dev]` so you can tell at a glance that Ledgr-dev replied, not the production bot.

### 4. Invite Ledgr-dev to a test channel

```
/invite @Ledgr-dev
```

Use **`/ledgr-dev settings`** (not `/ledgr`) — the dev app registers a separate slash command so it does not clash with production **Ledgr-QBS** in the same workspace.

Only invite Ledgr-dev to channels like `#qa-blockkit` or personal DMs. Never invite it to a channel where the production bot is already present or where real client data lives.

## HTTP dev server (optional)

To exercise the same FastAPI app Cloud Run serves:

```sh
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

| Endpoint | Expected without full creds |
|----------|-------------------------------|
| `GET /openapi.json` | 200 — process is up |
| `GET /healthz` | 503 `{"missing": ["SLACK_BOT_TOKEN", ...]}` until Slack vars are set |
| `POST /slack/events` | 500 without GCP ADC (lazy Firestore session service init) |

This is normal in a bare shell. Full Slack + Firestore handling needs ADC (`gcloud auth application-default login`), Slack tokens, and `GOOGLE_API_KEY` (AI Studio) for Gemini in dev.

## Running tests offline

```sh
uv run pytest                    # default fast unit suite
uv run pytest tests/eval/ -m eval -v   # document eval lane (needs GOOGLE_API_KEY for live cases)
./scripts/ledgr_eval_chat.sh     # chat eval lane (B* cases)
```

Integration tests mock Gemini, Slack, and Firestore — no network required for the default `pytest` target.

## Production

Cloud Run runs the **Ledgr-QBS** Slack app (`slack/manifest-qbs.json`) with production tokens from Secret Manager and `LEDGR_ENV=prod`. See `.env.prod.example` for the full variable reference — that file is documentation only; Cloud Run never reads it directly.

Deploy path: push to `main` → CI test gate → Cloud Run deploy. See [`deployment/README.md`](../deployment/README.md).

## Switching back

You don't need to switch. Keep both running simultaneously: local Ledgr-dev for iteration, Cloud Run prod for real customers. The separate bot tokens guarantee the two apps never respond to each other's events.

## Common pitfalls

### Session state serialization (`document_kind`, credit notes)

ADK persists invoice objects as plain dicts in Firestore session state. All
round-trips go through `accounting_agents/normalized_invoice_codec.py` — the
single codec for `NormalizedInvoice` / `BankStatement`.

**Pitfall:** if a field (e.g. `document_kind`) is missing from the codec,
credit notes lose their sign on export after a HITL pause/resume — amounts stay
positive when they should be negative. The exporters key off `document_kind ==
"credit_note"` (`invoice_processing/export/exporters.py`).

When adding fields to `NormalizedInvoice`, update **both** the dataclass and the
codec (`invoice_to_dict` / `dict_to_invoice`). Regression tests live in
`tests/test_nodes.py::test_normalized_invoice_codec_*`.

### Socket mode + OAuth env vars

If `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET` are set (even empty strings), Bolt
may ignore `SLACK_BOT_TOKEN`. Socket mode strips these in `_main_async()` after
imports — do not re-add them to `.env` for local dev.

### Firestore namespace

| `LEDGR_ENV` | `LEDGR_FIRESTORE_NAMESPACE` | Collections |
|-------------|----------------------------|-------------|
| dev (shared project) | `dev` | `dev_clients`, `dev_sessions`, … |
| prod | unset | `clients`, `sessions`, … |

### Engine harness vs live graph

`invoice_processing/pipeline.py::process_batch` is the hermetic harness — **not**
called by `slack_runner` in production. Engine fixes must be mirrored in
`accounting_agents/nodes.py` (or shared functions both call). See ADR-0001.

### Scratch / real data

`scratch/` and `playground_profile.json` are gitignored. Do not commit real
client PDFs or profiles.
