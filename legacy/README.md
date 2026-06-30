# Legacy packages (retirement in progress)

`accounting_agents/` and `invoice_processing/` are **deprecated**. The live
agent spine, billing, policy rulebook, and runtime entry now live in
`ledgr_agent/`.

## Status

| Package | Role | Replacement |
|---------|------|-------------|
| `ledgr_agent/` | Self-sufficient ADK agent | **Active** |
| `accounting_agents/` | Legacy Slack Bolt runtime + old graph | `ledgr_agent.runtime` (cutover in progress) |
| `invoice_processing/` | Hardcoded extraction factory | `ledgr_agent` light read + `policies/ledger/` |

## Migration gate

Before `git mv` into this folder:

1. `ledgr_agent` has zero imports from either legacy package (grep gate — done).
2. Fast unit suite green (`uv run pytest`).
3. Production Slack path uses `ledgr_agent` tools for document processing + billing.

After a sprint of prod use on the `ledgr_agent`-only path, delete the archived
trees per ADR retirement plan.
