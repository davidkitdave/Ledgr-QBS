# Eval (legacy routing stub)

The **live invoice eval** lives in [`ledgr_agent/eval/`](../../ledgr_agent/eval/).
See [ADR-0033](../../docs/adr/0033-reference-free-ledgr-agent-eval.md).

This directory keeps:

- [`eval_routing.py`](eval_routing.py) — single-lane route to `ledgr_agent.agent`
- [`datasets/anonymisation-note.md`](datasets/anonymisation-note.md) — PII policy

## Run the eval

```bash
# Flywheel (agents-cli)
./scripts/ledgr_eval_light.sh

# Pytest (ADK AgentEvaluator, needs GOOGLE_API_KEY)
uv run pytest ledgr_agent/eval/test_h_ledgr_light_live.py -m eval -v
```

Default `pytest` ignores `ledgr_agent/eval/` and this directory's retired cases.
