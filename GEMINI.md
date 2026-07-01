# Coding Agent Guide

## Prerequisites

Install the CLI (one-time):
```bash
uv tool install google-agents-cli
```

---

## Development Phases

### Phase 1: Understand Requirements
Before writing any code, understand the project's requirements, constraints, and success criteria.

### Phase 2: Build and Implement
Implement agent logic in `ledgr_agent/` and Slack I/O in `ledgr_slack/`. Use `agents-cli playground` for interactive testing.

### Phase 3: The Evaluation Loop
Start with eval cases in `ledgr_agent/eval/`, run `scripts/ledgr_eval_light.sh` or `agents-cli eval generate` + `grade`. Iterate until satisfied.

### Phase 4: Pre-Deployment Tests
```bash
uv run ruff check app ledgr_agent ledgr_slack tests
uv run pytest
```

### Phase 5: Deploy to Dev
**Requires explicit human approval.** Run `agents-cli deploy` only after user confirms.

---

## Development Commands

| Command | Purpose |
|---------|---------|
| `agents-cli playground` | Interactive local testing |
| `uv run pytest` | Hermetic unit + integration tests |
| `uv run pytest -m slow` | Bank ledger formula edge cases |
| `./scripts/ledgr_eval_light.sh` | Live Gemini eval (16 cases) |
| `uv run python -m ledgr_slack` | Socket Mode Slack bot (dev) |

---

## Live document path

Production Slack: `read_doc` → `build_sheets` → FY ledger delivery. Do **not** reference
`process_document_batch`, `accounting_agents`, or `invoice_processing` — removed.

See [docs/qa/testing-process.md](docs/qa/testing-process.md) and [CONTEXT.md](CONTEXT.md).

---

## Operational Guidelines

- **Code preservation**: Only modify code directly targeted by the user's request.
- **Run Python with `uv`**: `uv run python script.py`
- **ADK tool imports**: Import the tool instance, not the module.
- **Stop on repeated errors**: Fix root cause after 3+ identical failures.
