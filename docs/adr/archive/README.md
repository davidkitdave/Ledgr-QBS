# Archived ADRs

These records describe the **removed** stack:

- `accounting_agents/` — ADK Workflow graph, HITL, chat lane, Slack runner monolith
- `invoice_processing/` — classify → chunk → extract → categorize factory

That code was deleted **2026-07-01**. Production is **`ledgr_slack` + `ledgr_agent`** only.

| ADR | Why archived |
|-----|----------------|
| 0001 | Slim Workflow graph wrapping deterministic engine |
| 0003 | HITL via `request_input` in graph |
| 0006 | COA spreadsheet onboarding (superseded by 0036) |
| 0007 | HITL review/edit Slack surface |
| 0008 | Chat-lane standalone root agent |
| 0009 | Chat write tools + tool confirmation |
| 0010 | Scoped re-extract in factory |
| 0011 | Understand layer / Drive parity factory path |
| 0012 | Batch job queue (never implemented) |
| 0014 | Capture → Book → Verify puzzle |
| 0017 | Quality-gated HITL escalation |
| 0019 | Universal adapter (never implemented) |
| 0021 | RouteDecision coordinator retirement |
| 0013 | Native ADK adoption matrix (chat lane) |
| 0015 | Eval-driven prompt loop (superseded by 0033) |
| 0023 | Two-lane eval (factory + chat) |
| 0025 | Faithful extraction + COA confidence HITL |
| 0027 | Direction LLM read + vendor floor |
| 0029 | Multi-doc fan-out chunking |

Do not implement new features against these files.
