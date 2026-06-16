# Legacy agent archive

Pre-consolidation rule engines and research CLIs moved here. They are **not**
wired into the live Slack / ADK graph (`accounting_agents/slack_runner.py`).

| Path | Purpose |
|------|---------|
| `invoice_processing/shared_libraries/alf_engine.py` | ALF rule engine (tests + rule_writer only) |
| `invoice_processing/shared_libraries/acting/general_invoice_agent.py` | Standalone 9-agent CLI |
| `invoice_processing/shared_libraries/investigation/investigate_agent_reconst.py` | Offline audit batch tool |

Thin shims remain at the original import paths where tests still reference them.
