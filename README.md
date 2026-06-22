# Ledgr-QBS

Slack-native accounting agent for bookkeeping firms. Firms install the Ledgr
app into their workspace; each client channel gets document processing (classify →
understand → categorise → tax → workbook), human review when needed, and chat
Q&A — all backed by a slim ADK `Workflow` graph and a deterministic engine.

**Domain vocabulary:** [`CONTEXT.md`](CONTEXT.md)  
**Architecture decisions:** [`docs/adr/`](docs/adr/)  
**Agent / CI notes:** [`AGENTS.md`](AGENTS.md)

## Runtime surfaces

| Surface | Entrypoint | When to use |
|---------|------------|-------------|
| **Live Slack (prod)** | `app.main:app` on Cloud Run | Customer workspaces via HTTP (`POST /slack/events`) |
| **Live Slack (dev)** | `slack_bot.py` → `accounting_agents.slack_runner.main()` | Local socket mode against LEDGR-DEV |
| **Engine harness** | `invoice_processing/pipeline.py` | Hermetic unit/eval runs — injects LLM steps, no creds |
| **Chat eval lane** | `tests/eval/` + `scripts/ledgr_eval_chat.sh` | Conversational B* cases via agents-cli |

The live runtime is **`accounting_agents/slack_runner.py`** (graph + HITL + Slack I/O).
`invoice_processing/pipeline.py` is **not** the production path — it is the
deterministic engine harness used by tests and eval (see ADR-0001).

## Quick start (developers)

```bash
uv sync
cp .env.example .env   # fill Slack tokens, GOOGLE_API_KEY, LEDGR_FIRESTORE_NAMESPACE=dev
uv run pytest          # fast hermetic suite (~680 tests)
```

**Socket-mode bot (recommended for Slack iteration):**

```bash
uv run python slack_bot.py
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
  in a bare dev shell, not a crash.
- `POST /slack/events` — needs GCP Application Default Credentials (Firestore
  session service) plus real Slack tokens.

## Project layout (high level)

```
accounting_agents/     # Live ADK graph, slack_runner, HITL, sessions
invoice_processing/    # Deterministic engine (classify, extract, export)
app/                   # FastAPI entry (main.py), Block Kit builders, onboarding
tests/                 # Unit suite (default pytest target)
tests/eval/            # Two-lane eval (document + chat) — see tests/eval/README.md
docs/adr/              # Architecture decision records
slack/                 # Slack app manifests (prod + dev)
```

## Evaluation

Ledgr has **two eval lanes** (ADR-0023). Operational guide:
[`tests/eval/README.md`](tests/eval/README.md).

```bash
uv run pytest tests/eval/ -m eval -v     # document lane (PDF → graph)
./scripts/ledgr_eval_chat.sh             # chat lane (B* cases)
```

## Deployment

Production deploys via GitHub Actions on push to `main`: ruff + pytest gate →
SHA-tagged Docker image → `gcloud run deploy` with live traffic. Manual rollback
via `workflow_dispatch`. Details: [`deployment/README.md`](deployment/README.md)
and ADR-0018.

One-off manual deploy (local tree): `bash scripts/deploy-prod.sh`.

## Legacy / research code

The original Invoice Processing ALF demo (dual-mode inference/learning agent,
`adk web invoice_processing`) is **retired** from the live path. Research
artifacts live under [`legacy/`](legacy/README.md). Do not follow the old
`adk web` instructions in archived docs — they target code that is no longer
wired to Slack.
