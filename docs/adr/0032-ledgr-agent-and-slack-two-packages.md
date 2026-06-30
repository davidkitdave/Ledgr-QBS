# 0032 — `ledgr_agent` is a pure agent library; `ledgr_slack` owns the frontend; one Cloud Run service

- **Status:** Accepted
- **Date:** 2026-06-29
- **Deciders:** Ledgr team
- **Relates to:** ADR-0026 (AI reads, rules apply on a lean LlmAgent),
  ADR-0030 (one direct call beats the chunked factory),
  ADR-0031 (light-path minimum policy ladder),
  ADR-0022 (Firestore dev/prod isolation namespace).
- **Branch / evidence:** `feat/minimal-extract-control-experiment`,
  commits `d1ccf2e` → `08d189c`.

## Context

ADR-0030/0031 landed the lean serving path: one `read_doc` Gemini call +
deterministic `build_sheets` projection, wired live into the Slack runner. It
works, but it shipped as one uncommitted working-tree change with two coupling
problems that block the eventual archival of the old code:

1. **`ledgr_agent` deep-imported the legacy packages.** The three frontend
   modules (`runtime/{slack_shell,delivery,session}.py`) reached into
   `accounting_agents` and `invoice_processing`. As long as the *agent* package
   imports the *frontend* + *legacy pipeline*, you cannot archive either without
   breaking the agent.
2. **No recorded boundary.** Nothing said which parts of the 6682-line
   `accounting_agents/slack_runner.py` + `invoice_processing` the live path
   actually needs, versus the old graph / chat / HITL machinery that is dead
   weight. Archiving blind is unsafe.

The user's directive: *"a very minimal code to let the ledgr agent and slack
work perfectly… so later we can move `invoice_processing` and `accounting_agents`
into a legacy/archive folder without breaking anything."* And: **split now,
archive next.**

### ADK grounding (verified via adk-docs MCP, `adk.dev/deploy/cloud-run`)

A **custom FastAPI app that embeds the agent and calls `Runner` in-process is the
documented Cloud Run pattern** ("if you want to embed your agent within a custom
FastAPI application"). Our `ledgr_agent/agent.py` (`root_agent`) + `app/main.py`
entry + Dockerfile already match that layout. So **one service is correct**; the
two-package split is purely a code-boundary improvement, not a deployment change.
A future two-service split (frontend calls the agent over HTTP / the ADK API
server) then becomes a small change rather than a rewrite.

## Decision

**1. `ledgr_agent` is a pure agent library — zero `accounting_agents` /
`invoice_processing` imports.** Enforced by a gate:
`rg "accounting_agents|invoice_processing" ledgr_agent/ -g '*.py'` returns
nothing (one docstring line in `billing.py` excepted). It contains the agent
(`agent.py`, `app.py`), the two tools (`read_doc`, `build_sheets`), billing, and
the pure `internal/*` projection/normalisation/skill helpers.

**2. `ledgr_slack` is the frontend I/O package.** It owns the Slack-facing glue:
`slack_shell.py` (file-upload → `Runner` → delivery), `delivery.py` (workbook →
ledger payload), `session.py` (state-delta seeding), and a copied pure helper
`ledger_doc_identity.py`. It calls `ledgr_agent` in-process.

**3. One Cloud Run service.** `app/main.py` imports `build_fastapi_app` from
`ledgr_slack`. Handlers, ledger store, sessions, credit adapter, and UX live in
`ledgr_slack/`. Socket Mode: `python -m ledgr_slack`.

## Live path (2026-07)

```
app/main.py → ledgr_slack.build_fastapi_app
  → file upload → process_file_via_ledgr_agent
  → ledgr_agent.read_doc → ledgr_agent.build_sheets
  → ledgr_slack.delivery → SlackLedgerStore
```

## Legacy removal (2026-07-01)

The `accounting_agents` / `invoice_processing` trees and the ADK Workflow graph
were **deleted**. Archived ADRs: [`docs/adr/archive/`](../adr/archive/).

Chat Q&A (`assistant_app`) is **not** ported; document lane only until rebuilt
clean on `ledgr_agent`.

## Consequences

- `ledgr_agent` is independently importable; the frontend is swappable.
- One Cloud Run service; no behaviour change from the user's perspective.
- Import gate: `tests/test_import_isolation.py` — live packages must not import
  `legacy`, `invoice_processing`, or `accounting_agents`.

## Verification

- `uv run pytest` — unit suite green.
- `rg "accounting_agents|invoice_processing|from legacy" ledgr_agent/ ledgr_slack/ app/ -g '*.py'` → empty (docstrings excepted).

