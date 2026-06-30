# QA documentation

Historical QA runbooks, session notes, and architecture maps for the **removed**
`accounting_agents` / `invoice_processing` stack are in [`archive/`](archive/).

## Live testing today

| What | How |
|------|-----|
| Unit suite | `uv run pytest` |
| Agent eval (live Gemini) | `scripts/ledgr_eval_light.sh` |
| Slack Socket Mode (dev) | `uv run python -m ledgr_slack` |
| HTTP health | `uv run uvicorn app.main:app --port 8080` then `GET /healthz` |

See [AGENTS.md](../../AGENTS.md) and [docs/dev-environment.md](../dev-environment.md).
