# 0003 — HITL via ADK `RequestInput` in the slim graph (not Tool Confirmation, not Block Kit)

- **Status:** Accepted
- **Date:** 2026-06-13
- **Deciders:** David (developer)

## Context

Low-confidence extractions must be reviewed by a human in Slack before they are
committed to a workbook. The Engine already produces the trigger signals
(`reconciled`, per-line tax confidence, flagged), and `accounting_agents/nodes.py`
already has the `_needs_review` logic and an `approval_gate`.

ADK offers (verified against the ADK MCP docs) **three** ways to obtain human input:

1. **`RequestInput` graph node** — pauses a `Workflow` graph until a human responds.
   `accounting_agents/hitl.py` already implements the full Slack↔ADK bridge:
   a Firestore correlation doc, `resume_session` feeding the decision back into
   `runner.run_async(..., invocation_id=...)` as a function-response, and
   idempotency markers for double-clicks. Proven with **Firestore sessions**.
2. **Tool Confirmation** (`FunctionTool(require_confirmation=True)` /
   `tool_context.request_confirmation(hint, payload)`) — works on a **bare
   `LlmAgent`, no graph**, and supports remote confirmation over a chat channel
   via a `FunctionResponse` named `adk_request_confirmation`.
3. **Block Kit only** — flag rows, post ✅/✏️ buttons, manage pending state
   ourselves; no ADK HITL primitive at all.

The decisive constraint: Tool Confirmation is **experimental and explicitly
unsupported with `DatabaseSessionService` / `VertexAiSessionService`** (persistent
sessions). Production runs on Firestore sessions. So the one path that both is
native ADK HITL **and** survives production persistence is the graph `RequestInput`
path — which is also already implemented in `hitl.py`.

## Decision

Use **ADK 2.0 `RequestInput`** as a node in the slim processing graph (ADR-0001):
`Engine node → approval node (RequestInput, only when `_needs_review`) → deliver
node`. The approval node runs **no LLM**. Bridge async Slack approvals to the paused
invocation by **reusing `accounting_agents/hitl.py`** (Firestore interrupt doc +
`resume_session`), wiring one Slack Bolt `@app.action` handler that resolves the
human's approve/edit into the resume payload. An approve-with-edit is persisted as a
**Correction** (ADR-0004).

## Consequences

- HITL is a first-class, inspectable workflow step with durable pause/resume across
  the (possibly long) human delay, backed by Firestore.
- Most of the work already exists (`hitl.py`, `_needs_review`, `approval_gate`); the
  net new work is wiring the Bolt action handler and the review card.
- This is a second reason the runtime root must be a graph (see ADR-0001); a bare
  `LlmAgent` is not viable for production HITL here.
- We forgo Tool Confirmation's simplicity, accepting graph surface area, because
  Tool Confirmation cannot persist with our session service today. Revisit if ADK
  lifts the `DatabaseSessionService`/`VertexAiSessionService` limitation.

## Alternatives considered

- **Tool Confirmation on a bare `LlmAgent`** — simplest and graph-free, but
  experimental and unsupported with persistent sessions; rejected for production.
- **Block Kit only** — no ADK HITL primitive; duplicates the resume/idempotency
  machinery `hitl.py` already provides; rejected.
