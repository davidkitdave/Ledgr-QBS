# Ledgr — ADK Consolidation (retire the dead duplicate, unify prod)

**Goal:** One Engine, one ADK root (the live `accounting_agents` graph), one Slack
runtime shared by local (socket) and prod (FastAPI). Remove the documented dead
duplicate so changes can't drift across two orchestrations, and so production gets
the graph + HITL instead of the old `process_batch` path.

**Authority:** ADR-0001 (corrected addendum 2026-06-14) — *"the graph is the LIVE
runtime"*; ROADMAP founder note *"consolidate first."* This plan executes the
corrected keep/retire list.

**Live (KEEP):** `accounting_agents/` (graph + `slack_runner` + `hitl` + `sessions`),
`invoice_processing/` engine (`extract`/`classify`/`export`), `app/slack_app.py`'s
live pure handlers (onboarding/commands), `app/blocks.py`, `app/commands.py`.

**KEEP as eval/test harness (not live):** `invoice_processing/pipeline.py::process_document`
— `eval/client_eval.py` + `eval/ledger_eval.py` + hermetic tests drive it. Document
clearly that it is NOT the live runtime (live = the graph). `process_batch` may be
removed once its only consumers (dead code) are gone.

**Test commands:** `.venv/bin/pytest -q`; lint `.venv/bin/ruff check accounting_agents app invoice_processing eval`.

> Each task ends green (full suite + ruff). Commit per task. Branch: `refactor/adk-consolidation`.

---

## Task 0 — Branch + baseline
- [ ] Create branch `refactor/adk-consolidation`; confirm full suite green as baseline.

## Task 1 — Delete zero-importer orphaned agents
**Why:** dead, confusing parallel ADK definitions; zero importers (verified).
- Files: `accounting_agents/invoice_agent.py`, `accounting_agents/bank_feed_agent.py`,
  `accounting_agents/bank_statement_extractor_agent.py`, `invoice_processing/agent.py`,
  and any tests that import them.
- [ ] `grep` each module path → confirm zero non-test importers, delete file(s).
- [ ] Remove/trim tests that target only these (`test_*agent*` for the deleted ones).
- [ ] Gate: suite green; `grep` proves no remaining references.

## Task 2 — Delete the second ADK root: `ledgr_coordinator/`
**Why:** redundant second App root (ADR-0001 "DELETE whole").
- Files: `ledgr_coordinator/` (whole dir incl. `agent.py`, `tools.py`, `prompt.py`,
  `fast_api_app.py`), `tests/test_coordinator_tools.py`, `tests/integration/test_agent.py`.
- [ ] Delete the package + its tests. Confirm nothing live imports `ledgr_coordinator`.
- [ ] Gate: suite green; no `ledgr_coordinator` references remain.

## Task 3 — Retire the dead Slack/`process_batch` path
**Why:** `app/processing.py` (`process_batch`) is unreachable from the live runner
(ADR-0001 addendum); `app/socket_run.py` is the old socket entry superseded by
`slack_runner`. `app/slack_app.py` is MIXED — keep its live pure handlers.
- Files: `app/processing.py` (delete), `app/socket_run.py` (delete), `app/slack_app.py`
  (surgically remove `build_app`, `fastapi_app`, `handle_file_share`/file-share path,
  and the `process_shared_files` call — KEEP `handle_setup_open`, `handle_onboarding_submit`,
  `handle_ledgr_command`, dedupe/redirect guards, and anything `slack_runner` imports),
  tests `test_app_processing.py`, `test_app_archive.py` (trim process_batch parts),
  `test_app_config.py` (socket_run import).
- [ ] Verify exactly which symbols `accounting_agents/slack_runner.py` imports from
  `app.slack_app` / `app.blocks` / `app.commands` — those MUST survive untouched.
- [ ] Remove the dead path + adapt/remove its tests. Do NOT break the live handlers.
- [ ] Gate: suite green; `grep` proves no live reference to `app.processing` /
  `process_shared_files` / `app.socket_run`.

## Task 4 — Unify prod onto the live graph (CRITICAL, do last)
**Why:** `app/main.py → app.slack_app.fastapi_app` runs the OLD path (no graph, no
HITL). Prod must drive the SAME graph as the socket runner.
- Design: serve `accounting_agents.slack_runner.build_async_app(...)` (the AsyncApp:
  file→`process_file_event`, message→`answer_question`, approve/edit/reject HITL) over
  HTTP via slack_bolt's `AsyncSlackRequestHandler`, mounted on a FastAPI app at
  `POST /slack/events`. Build the Runner via `build_runner()` so prod and local share
  `build_async_app`.
- Files: a new prod entry (e.g. `accounting_agents/fastapi_app.py` or rework
  `app/main.py`), `app/main.py` (point to it), tests.
- [ ] Implement the FastAPI-over-AsyncApp serving; reuse `build_runner` + `build_async_app`.
- [ ] Test: prod entry builds a Runner whose app is `accounting_agents.agent.app`
  (the graph), wires the async handlers, and exposes `POST /slack/events` (assert with
  fakes/monkeypatch — no live Slack/network).
- [ ] Gate: suite green; prod and local provably share one runtime/graph.

## Task 5 — Tidy `pipeline.py` + docs
- [ ] If `process_batch` has no remaining consumers after Tasks 2–3, remove it (keep
  `process_document`); otherwise keep + add a module docstring note: *"engine/eval
  harness — NOT the live runtime; live = accounting_agents graph."*
- [ ] Update ADR-0001 status → consolidated (date), and ROADMAP consolidation row.
- [ ] Gate: suite green; `ruff check accounting_agents app invoice_processing eval` clean.

## Final verification
- [ ] `.venv/bin/pytest -q` green.
- [ ] `ruff check accounting_agents app invoice_processing eval` clean.
- [ ] `grep` proof: no importers of deleted modules; only ONE App root
  (`accounting_agents/agent.py`); prod + local both route through `build_async_app`.
- [ ] End state matches ADR-0001: one Engine, one ADK root, one Slack runtime.
