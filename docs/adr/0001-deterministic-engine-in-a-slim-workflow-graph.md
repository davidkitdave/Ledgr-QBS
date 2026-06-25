# 0001 — Deterministic Engine as one node in a slim Workflow graph

> **⚠️ Superseded in part by [ADR-0026](0026-ai-reads-rules-apply-on-a-lean-llmagent.md) (2026-06-24).**
> *Retained:* the engine is deterministic and is never re-expanded into per-step LLM nodes.
> *Superseded:* "the runtime root is a slim `Workflow` graph" — the root is now the lean
> `ledgr_agent` `LlmAgent` and the engine is a **tool**, not a graph node. The "bare
> `LlmAgent` root, Engine as a tool" alternative rejected below is the one now adopted,
> because the Firestore interrupt bridge (`hitl.py`) — not a graph `RequestInput` node —
> provides HITL pause/resume.

- **Status:** Accepted — **consolidation implemented 2026-06-14** (see "Implementation" below)
- **Date:** 2026-06-13
- **Deciders:** Ledgr team

> **Implementation (2026-06-14).** The corrected keep/retire list is done:
> deleted the orphaned agents (`invoice_agent`, `bank_feed_agent`,
> `bank_statement_extractor_agent`, `invoice_processing/agent.py`) and the second
> root `ledgr_coordinator/`; retired the dead transport (`app/processing.py`
> `process_batch`, `app/socket_run.py`, and `build_app`/`fastapi_app` in
> `app/slack_app.py`); unified prod onto the live graph — `app/main.py` now serves
> `accounting_agents.slack_runner.build_fastapi_app()` (graph + HITL via
> `AsyncSlackRequestHandler`), so local (socket) and prod (HTTP) share one runtime.
> ADR-0006 COA-upload (path A) was ported into the live runner
> (`_is_coa_upload` → `run_coa_ingest`). `invoice_processing/pipeline.py` is kept
> only as the engine/eval harness (not live). Deeper "engine as a single node"
> (ROADMAP Stage 3) remains future work.

## Context

Three runtimes coexisted in the repo:

1. `invoice_processing/pipeline.py` — a **plain-Python** deterministic pipeline
   (`process_batch`): classify → extract → categorise → tax → workbook. LLM is
   called only for the multimodal classify/extract steps. Trusted, well-tested,
   and the one wired into live Slack.
2. `ledgr_coordinator/` — a lean ADK `LlmAgent` whose `process_documents` tool
   delegates to that pipeline.
3. `accounting_agents/` — a full ADK 2.0 `Workflow` graph that re-implemented the
   pipeline as a chain of **per-step LLM nodes** (classify_node, extract_invoice_node,
   categorize_node, tax_node, extract_bank_node), plus a coordinator, HITL, and
   sessions.

Runtime (3) was built and tested but, when exercised for real extraction, **burned
far more tokens and produced worse results** than the deterministic pipeline —
because every step was an LLM-driven graph node. The developer reverted to the
deterministic pipeline for the actual work.

The naive conclusion was "retire `accounting_agents`, keep the bare `LlmAgent`
coordinator as the single root." Verification against the ADK MCP docs showed that
conclusion is wrong for the HITL requirement (see ADR-0003): **ADK's `RequestInput`
human-input node only works inside a `Workflow` graph**; a bare `LlmAgent` cannot
host it. The ADK docs also confirm graph nodes can be **plain deterministic
functions** ("run chains of functions without AI") and that a node may itself be a
Tool, an Agent, or another Workflow.

## Decision

The runtime root is a **slim ADK 2.0 `Workflow` graph** — structurally the
`accounting_agents` skeleton (Coordinator `LlmAgent` entry node → router → branches
→ approval → deliver) — **with the per-step LLM extraction nodes replaced by a
single deterministic Engine node** that calls `invoice_processing.pipeline.process_batch`.

- The **Engine** (the plain-Python pipeline) is the system's extraction/export
  authority and is **never** re-expanded into a chain of LLM nodes.
- The **Coordinator `LlmAgent`** spends LLM tokens only on what genuinely needs a
  model: understanding the user's message and choosing a branch (the unavoidable
  cost of being a conversational "teammate").
- The token-burning extraction nodes in `accounting_agents/nodes.py` are retired;
  the graph skeleton, router, approval, sessions, and `hitl.py` are kept.

## Consequences

- **Token cost collapses** to: one routing LLM call per user turn + the pipeline's
  own (already minimal) classify/extract calls. No per-step graph-node LLM tax.
- The proven deterministic pipeline keeps full ownership of extraction quality
  (which is then driven by eval — see the build plan), so correctness does not
  regress.
- Native ADK HITL (ADR-0003) becomes available because the root is a graph.
- `ledgr_coordinator/` (bare `LlmAgent` + tools) is **not** the production root.
  Its lean design informs the Coordinator node, but it cannot host `RequestInput`.
- We accept carrying a graph runtime (more framework surface than a bare agent) in
  exchange for native HITL and explicit, inspectable routing.

## Consolidation (keep / slim / delete)

Verification found the repo carries **two ADK roots, two Slack transports, three
Engine orchestrators, and five entrypoints** — leftovers from the abandoned agentic
experiment. Crucially, `accounting_agents` is the **registered** ADK agent
(`pyproject.toml` and `agents-cli-manifest.yaml` set `agent_directory =
"accounting_agents"`) and holds the HITL machinery worth keeping — so it is
**slimmed in place**, not deleted. The live `app/` imports neither leftover, so
removal is safe for the running bot.

- **KEEP — Engine:** `invoice_processing/` (untouched).
- **KEEP — Slack transport:** `app/` (`slack_app.py`, `socket_run.py`, `main.py`,
  `processing.py`). `processing.py` is rewired to run the graph via a `Runner`
  instead of calling `process_batch` directly.
- **SLIM IN PLACE → the one ADK root:** `accounting_agents/`.
  - *Keep:* `agent.py` graph skeleton, `hitl.py`, `sessions.py`, `config.py`, and
    the approval / `_needs_review` / `ApproveDecision` parts of `nodes.py`.
  - *Remove:* the per-step LLM extraction nodes in `nodes.py`, `invoice_agent.py`,
    `bank_feed_agent.py`, `bank_statement_extractor_agent.py`, `qa_agent.py`
    (salvage its prompt wording into the coordinator node), the duplicate
    `slack_runner.py` and `fast_api_app.py`.
  - *Replace* the removed extraction nodes with **one deterministic Engine node**
    calling `process_batch`.
- **DELETE whole:** `ledgr_coordinator/` (redundant second root) and `slack_bot.py`
  (launcher for the leftover transport).
- **Tests:** keep/adapt the salvage tests (`test_hitl_roundtrip`,
  `test_resume_idempotency`, `test_sessions`, `test_graph_wiring`); remove the tests
  for deleted code (`test_nodes` extraction, `test_qa_agent`, `test_slack_runner`,
  `test_coordinator_tools`). Suite must be green after each step.

End state: **one Engine, one ADK root (the slim graph), one Slack transport, one
entrypoint each** (FastAPI prod + socket local).

## Alternatives considered

- **Bare `LlmAgent` root, Engine as a tool** — simplest, but cannot host
  `RequestInput`; rejected once HITL was required (ADR-0003).
- **Keep `accounting_agents` as-is (LLM extraction nodes)** — the token-burner;
  rejected.
- **Rebuild the pipeline internals as ADK SequentialAgent nodes** — reintroduces
  the per-step structure that caused the burn for no benefit; rejected.

## Addendum (2026-06-14) — correction: the graph is the LIVE runtime

The original Consolidation section above got **which runtime is live backwards.**
Verified against the run command (`local-bot-run-and-live-state`: the bot is started
via `from accounting_agents.slack_runner import main; main()`), `slack_bot.py` (a thin
shim whose docstring states *"the real Slack ↔ ADK driver now lives in
accounting_agents.slack_runner"*), and the code:

- **LIVE = the `accounting_agents` graph.** `accounting_agents/slack_runner.py` is the
  running AsyncApp: it owns Slack I/O, registers `file_shared`/`message` →
  `process_file_event` → runs `accounting_agents.agent` (the graph), and the
  `approve`/`edit`/`reject` HITL actions. It **reuses `app/`** — importing
  `handle_ledgr_command`, `handle_onboarding_submit` from `app.slack_app` and
  `approval_card_blocks` from `app.blocks`. So `app/slack_app.py` (onboarding/commands)
  and `app/blocks.py` **are live**, called *by* the graph runner.
- **DEAD duplicate = the deterministic `process_batch` path.** `app/processing.py`
  (`process_batch`) is **not reachable from the live runner** (zero references from
  `accounting_agents` → `app.processing`). It is only reachable via `app/socket_run.py`
  / `app/main.py` (the FastAPI/Cloud-Run entrypoint), which the developer does not run.

**Corrected keep / retire:**
- **KEEP (live):** `accounting_agents/` (graph + `slack_runner` + `hitl.py` + `sessions`),
  `app/slack_app.py` (onboarding/commands handlers), `app/blocks.py`, `app/commands.py`,
  and `invoice_processing/` (the engine the graph's nodes call).
- **RETIRE (dead duplicate):** `app/processing.py` (`process_batch`), `app/slack_app`'s
  own `build_app`/`fastapi_app`/file-share path, `app/socket_run.py`, and
  `ledgr_coordinator/`.
- **UNIFY:** the Cloud Run entrypoint (`app/main.py` → `app.slack_app.fastapi_app`)
  currently runs the OLD `process_batch` path — **no graph, no HITL.** It must be
  switched to drive the same graph as the socket runner, so local and prod share one
  runtime.

**The decision is unchanged** — deterministic engine + slim graph + native HITL. Only
the keep/retire *assignment* flips: the graph was never the leftover; it is production.
The slim-graph end state (engine as one node, no per-step LLM extraction) is the
target; today's graph is already close (its nodes call the deterministic engine
functions). `accounting_agents/nodes.py`'s extraction nodes are **not** ripped out —
they each wrap one deterministic engine call, which is the endorsed ADK 2.0
"functions as nodes" pattern.
