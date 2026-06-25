# ADR-0021: Deterministic document entry; retire the LLM `RouteDecision` coordinator

> **ℹ️ Amended by [ADR-0026](0026-ai-reads-rules-apply-on-a-lean-llmagent.md) (2026-06-24).**
> *Retained:* deterministic document entry — no LLM `RouteDecision`; in Slack the file event
> still deterministically triggers processing. *Superseded:* `root_agent = document_workflow`
> — the discoverable root is now the lean `ledgr_agent` `LlmAgent`, which calls the engine via
> the `process_document_batch` tool (the `Workflow` graph is retired).

Status: Accepted (2026-06-19) — root reassignment superseded by ADR-0026
Builds on (does NOT supersede) ADR-0008 (chat lane as a standalone root agent).
Supersedes the "unified chat-coordinator that delegates to the task document workflow"
ambition in the IDP→ERP plan (`bubbly-sniffing-flurry.md`, WS3 / draft ADR-0021).

## Context

The IDP→ERP plan proposed unifying the system under one `mode="chat"` coordinator at
the graph root that delegates to the document pipeline as a `mode="task"` sub-agent,
replacing the `RouteDecision` 3-way classifier (`document` / `question` / `unknown`),
`dynamic_router`, `help_node`, and the separate chat `assistant_app`. The user asked us
to **verify that design rather than trust it**. We did, against installed `google-adk`
2.2.0 and the official docs, and the result reverses the recommendation.

### What we verified (evidence, not assumptions)

1. **A `Workflow` cannot be a sub-agent of an LlmAgent.** Probe
   `scratch/probe_ws3_topology.py`: `LlmAgent(mode="chat", sub_agents=[Workflow(...)])`
   raises `ValidationError: sub_agents.0 ... input_type=Workflow` — a `Workflow` is not a
   `BaseAgent`. The Ledgr document pipeline IS a `google.adk.workflow.Workflow`.
2. **"Do not configure a root agent with the `mode` setting."** (official
   `adk.dev/workflows/collaboration`). The coordinator pattern is `Agent(sub_agents=[...])`
   with no mode; sub-agents carry `task`/`single_turn`.
3. **Task mode is disabled in graph-based Workflows in v2.0.0** (same doc). The graph
   Workflow world (Ledgr's pipeline) and the LlmAgent-coordinator-with-sub_agents world
   are separate; you cannot fuse them by dropping a mode flag.
4. **A `chat`-mode agent still cannot be a downstream graph node** (ADR-0008 holds):
   `_validate_chat_agent_wiring` raises (probe negative control confirmed).
5. **Production already routes deterministically, upstream of any LLM coordinator.**
   `slack_runner.build_runner` binds file uploads to `document_app`
   (`root_agent=document_workflow`, which starts directly at `classify_node`) and text to
   `assistant_app` (`root_agent=assistant_agent`). The `coordinator` LlmAgent,
   `RouteDecision`, `dynamic_router`, and `help_node` are reached ONLY by `adk web`
   (which discovers the module-level `root_agent = coordinator_graph`) and by the opt-in
   `LEDGR_USE_COORDINATOR=1` flag (off by default). **The live Slack path never touches
   them.**

So the "rigid `unknown` dead-end" the user disliked is an **adk-web/playground vestige**,
not a production behaviour — and the proposed unification is blocked by three independent
ADK constraints while also solving a problem production does not have.

## Decision

Keep ADK's two native surfaces, which ADR-0008 already established and which the evidence
vindicates:

- **Documents** run on `document_app` → `document_workflow` (graph Workflow,
  `single_turn`/function nodes — observable in adk web's Graph/Trace).
- **Chat** runs on `assistant_app` → the standalone `assistant_agent` (multi-turn, ADR-0008).

Routing between them is **deterministic and already lives in the Slack layer** (a file
event → document path; a text event → chat path). No LLM decides "is this a document?"

Concretely:

1. **Remove** `RouteDecision`, the `coordinator` LlmAgent classifier, `dynamic_router`
   (+ `_extract_intent`), `help_node`, the `coordinator_graph` Workflow, and the
   `ROUTE_DOCUMENT`/`ROUTE_QUESTION`/`ROUTE_UNKNOWN` constants.
2. **Point `adk web` at the document pipeline directly**: module-level
   `root_agent = document_workflow`. adk web is the surface for testing the *document
   agent*; chat is tested via `playground_runner --chat` / the assistant app (per
   `docs/qa/testing-strategy.md`).
3. **Retire `LEDGR_USE_COORDINATOR`**: `build_runner` always binds documents to
   `document_app`. One document entry, not two.
4. **Text-in-adk-web degrades honestly, not with an LLM mislabel.** A text-only turn
   reaches `classify_node`, whose deterministic `_load_pdf_bytes` returns a clear
   "upload a document; for questions use the assistant" message — a deterministic guard,
   never an `intent=unknown` classification.

Why this is "less is more": it deletes an LLM call, a router node, a dead help node, a
schema, three route constants, and a config flag — and removes a whole class of
mis-classification — while changing **zero** production code paths.

## Consequences

- One document entry (`document_workflow`), one chat entry (`assistant_agent`); the
  `unknown` dead-end is gone. adk web's Graph tab now renders the real document pipeline
  instead of a classifier the bot never runs.
- The Slack hot path is untouched (verified): `document_app` and `assistant_app` are
  unchanged. This is the low-risk change the project needs before the ERP-model work.
- Graceful handling of off-enum document types (statement_of_account, credit_note,
  expense_claim) is a SEPARATE, additive change (data-driven lanes in `lane_config.py`)
  tracked under WS3b / WS4 — it does not depend on this routing change.
- If a true single conversational coordinator is ever wanted, the ADK-native route is to
  rebuild the document pipeline as a `SequentialAgent` sub-agent under a no-mode root
  coordinator — a large rewrite (changes adk-web rendering + HITL/resumability) explicitly
  out of scope here and gated on real need, not on plan momentum.

## Verification

- Probe `scratch/probe_ws3_topology.py` (construction results above).
- Official docs `adk.dev/workflows/collaboration` (modes, root-mode caution, task-in-graph
  disablement).
- `slack_runner.build_runner` / `process_file_event` / `answer_question` call-site trace
  (Slack path independence).
- Regression gate: the suite stays at its baseline (1791 passed / 6 pre-existing failures)
  after the removal, with `test_graph_wiring.py` / `test_eval_routing.py` /
  `test_adk_web_qa.py` updated to assert the new deterministic topology.
