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
`ledgr_slack`. To stay minimal and non-breaking this pass, `ledgr_slack`
**re-exports** the existing `build_fastapi_app` from `accounting_agents.slack_runner`
— the 6682-line runner is **not untangled yet**; it stays behind the
`ledgr_slack` seam. Untangling it (physically moving the live handlers + infra
into `ledgr_slack`, leaving the legacy graph/chat/HITL behind to archive) is the
**next** pass, made mechanical by the map below.

## Archive-readiness map (evidence-based, from the live import surface)

The live serving path is `app/main.py → ledgr_slack.build_fastapi_app
(→ accounting_agents.slack_runner.build_fastapi_app) → file-upload handlers →
process_file_via_ledgr_agent → ledgr_agent Runner → ledgr_slack delivery →
SlackLedgerStore`. Tracing every import that path touches:

### INFRA TO KEEP (the live path depends on these — relocate into `ledgr_slack`, do **not** archive)

| Module / symbol | Role on the live path |
|---|---|
| `accounting_agents.config._ns` / `_env_prefix` | Firestore dev/prod namespace isolation (ADR-0022) |
| `accounting_agents.ledger_store.SlackLedgerStore` | FY-workbook ledger (`Purchase`/`Sales` + bank tabs); `_fresh_invoice_workbook`, `append_rows` |
| `accounting_agents.ledger_doc_identity` | Pure doc-identity / dedup helper (**already copied** into `ledgr_slack`) |
| `accounting_agents.lease_lock.FirestoreLeaseLock` | Cross-instance ledger write lock |
| `accounting_agents.sessions.FirestoreSessionService` | ADK session persistence |
| `accounting_agents.credit_delivery` → `wire_shared_credit_service`, `credit_block_message`, `resolve_firm_id_from_client` | Credit gating / charge-on-delivery / firm resolution |
| `invoice_processing.export.exporters` → `get_exporter`, `BankStatementExporter`, `normalize_software_key` | xlsx write used by `SlackLedgerStore` |
| `invoice_processing.export.client_context.FirestoreClientStore` | Per-channel client profile (FYE month, software, COA) |
| `invoice_processing.extract.partial_failure.format_partial_failure_note` | Partial-failure messaging |
| `slack_runner` live handlers: `build_fastapi_app`, `build_runner`, `_post_delivery_card`, `_record_processing_log`, `_apply_state_delta`, `_ensure_session`, `_per_doc_session_id`, `_SEM`, file-upload routing | The Slack frontend itself |

### LEGACY TO ARCHIVE (the live file→extract→deliver path does **not** use these)

| Module / symbol | Why it is dead weight on the live path |
|---|---|
| `accounting_agents.agent.assistant_app` + `build_chat_runner` + `app_mention`→chat routing | The separate chat Q&A agent — **archive (reset)**; rebuild Q&A clean later (see "Chat-lane decision") |
| `accounting_agents.assistant` + `accounting_agents.assistant_tools.*` | The 10 read-only Q&A tools + helpers — **archive (reset), do NOT port**; legacy baggage, rebuilt clean later |
| `accounting_agents.nodes` (`ApproveDecision`, `ReviewClarifyDecision`, segmentation, graph seams) | The old document-workflow graph; the lean tools replace it |
| `accounting_agents.hitl` (approval / review / clarify) | HITL is off; the lean path auto-delivers |
| The `invoice_processing` extraction factory (chunking, `ledger_extract`, `invoice_extractor`, classifiers) | Superseded by `read_doc` + `build_sheets` (ADR-0030). **Keep only** the small `export/` + `partial_failure` slice listed above. |
| `slack_runner` COA-confirm / approval / replace-hint / proactive-HITL handlers | Old interaction flows; not reached by the lean path |

The physical relocation (moving the "keep" infra into `ledgr_slack`, trimming
`slack_runner` to the live handlers, then `git mv` the rest to `legacy/`) is
**explicitly deferred** to the next pass. This map makes it mechanical: anything
in "keep" moves with the frontend; anything in "archive" can be cut once its tests
move with it.

## Chat-lane decision (decided 2026-06-29 — clean reset, do NOT port)

Today `app_mention` + text messages route to `assistant_app` (the chat agent),
which has ~10 data-grounded read tools (`pnl_for_fy`, `bank_totals`,
`summarize_by_category`, `gst_threshold_check`, `lookup_coa_account`, …). The
lean `ledgr_agent` has only `read_doc`, `build_sheets`, `read_credit_balance` —
tagging it yields conversation but **not** ledger answers.

**Decision: clean reset.** Do **not** port the old Q&A tools onto `ledgr_agent`,
and do **not** keep investing in `assistant_app`. The existing chat/Q&A lane was
shaped by the `accounting_agents` / `invoice_processing` build and carries
unnecessary baggage; rather than migrate it, it is **archived with the rest of
the legacy** and the Q&A is **rebuilt cleanly later**, on its own terms, when we
choose to add it back properly. No chat-lane code changes happen now — the lean
agent stays document-only; the reset lands in the archive pass.

This moves `assistant` / `assistant_tools` / `assistant_app` from
"archive-OR-port" to a hard **archive (reset)** in the map below.

## Consequences

- `ledgr_agent` is now independently importable and deployable later as a
  standalone ADK service; the frontend is swappable.
- No behaviour change this pass: same single Cloud Run service, same handlers,
  full suite **2181 passed / 6 skipped / 0 failed** at each commit.
- The next archival pass is now a documented, low-risk mechanical move rather
  than an excavation.

## Verification

- `rg "accounting_agents|invoice_processing" ledgr_agent/ -g '*.py'` → empty
  (docstring excepted).
- `import app.main`, `import ledgr_slack.{slack_shell,delivery,session}` → OK from
  a clean checkout of `08d189c`.
- `pytest tests/` → 2181 passed, 6 skipped, 0 failed.
