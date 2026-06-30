# Ledgr-QBS

Slack-native accounting agent for bookkeeping firms. Firms install the Ledgr
app into their workspace; each client channel gets document processing (one-shot
AI read → ERP workbook rows) backed by `ledgr_agent` tools and `ledgr_slack`
delivery.

**Domain vocabulary:** [`CONTEXT.md`](CONTEXT.md)  
**Architecture decisions:** [`docs/adr/README.md`](docs/adr/README.md) (current) · [`docs/adr/archive/`](docs/adr/archive/) (history)  
**Agent / CI notes:** [`AGENTS.md`](AGENTS.md)

## Runtime surfaces

| Surface | Entrypoint | When to use |
|---------|------------|-------------|
| **Live Slack (prod)** | `app.main:app` on Cloud Run | Customer workspaces via HTTP (`POST /slack/events`) |
| **Live Slack (dev)** | `python -m ledgr_slack` or `slack_bot.py` | Local Socket Mode against LEDGR-DEV |
| **Agent eval** | `scripts/ledgr_eval_light.sh` | Reference-free PDF eval for `ledgr_agent` |

The live runtime is **`ledgr_slack` + `ledgr_agent`** only. See ADR-0032.

## Quick start (developers)

```bash
uv sync
cp .env.example .env   # fill Slack tokens, GOOGLE_API_KEY, LEDGR_FIRESTORE_NAMESPACE=dev
uv run pytest          # fast hermetic suite
```

**Socket-mode bot (recommended for Slack iteration):**

```bash
uv run python -m ledgr_slack
```

See [`docs/dev-environment.md`](docs/dev-environment.md) for the full dev/prod split,
Firestore namespace isolation (ADR-0022), and troubleshooting.

**HTTP server (same app Cloud Run serves):**

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

- `/openapi.json` — always 200 (proves the process is up).
- `/healthz` — 200 when Slack HTTP config is present; **503 with `{"missing": [...]}`**
  until `SLACK_BOT_TOKEN` / signing secret / OAuth vars are set. This is expected
  in a fresh checkout.

## Packages

| Package | Role |
|---------|------|
| `ledgr_agent/` | ADK agent library: `read_doc`, `build_sheets`, billing, eval |
| `ledgr_slack/` | Slack frontend, ledger store, export, client profiles |
| `app/` | HTTP shell (`main.py`), Block Kit blocks, onboarding modals |

## Eval

```bash
scripts/ledgr_eval_light.sh
```

Live Gemini eval configs live under `ledgr_agent/eval/`.
