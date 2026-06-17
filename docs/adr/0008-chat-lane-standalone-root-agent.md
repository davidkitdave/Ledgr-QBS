# ADR-0008: The chat lane runs as a standalone root agent, outside the coordinator graph

Status: Accepted (2026-06-15)
Supersedes the chat-side assumption in the master plan §7 Step 1 ("just drop `single_turn`").

## Context

The master plan (`docs/qa/2026-06-15-ledgr-intelligent-agent-masterplan.md`) Step 1 calls for the
chat helper (`qa_agent`) to become genuinely multi-turn ("remembers the thread") by dropping
`mode="single_turn"`. Today `qa_agent` is wired as a node in the `coordinator_graph` Workflow,
reached from the `dynamic_router` node, and each question runs in its own throwaway session
(`{channel}:q:{message_ts}`), so there is no conversational memory.

We verified what ADK 2.2.0 actually permits — three independent, agreeing sources:

1. **Official docs — graphs/routes:** "You can add LlmAgents to graph-based workflows, however they
   must be set to a **task or single-turn mode**."
2. **Official docs — workflows/collaboration:** `chat` mode is for **subagents under a coordinator**,
   not graph nodes; "Do not configure a root agent with the `mode` setting"; and **`task` mode is
   disabled in graph workflows in v2.0.0** — leaving `single_turn` as the only usable graph-node mode.
3. **Installed `google-adk` 2.2.0 source** — `workflow/_graph.py:520-538` (`_validate_chat_agent_wiring`)
   raises `ValueError` if a `mode='chat'` `LlmAgent` has an incoming edge from any non-START node.

Conclusion: a chat agent that sees session history **cannot** be a downstream node in the
coordinator graph. The plan's literal "drop `single_turn` in place" is infeasible — it would crash
graph construction. This is the structural reason, not a preference.

## Decision

The chat lane (`qa_agent` → `accounting_agent`) becomes a **standalone root `LlmAgent`** run by its
own `Runner` (its own `App`), **outside** the `coordinator_graph`:

- As a root agent it carries **no `mode`** setting, so `include_contents='default'` — it sees the full
  session history (multi-turn) per ADK's standard behaviour.
- The Slack text path (`answer_question`) uses a dedicated **per-thread chat session**
  (`{channel}:chat:{thread_ts}`), reused across turns, kept **separate** from document-processing
  sessions so pipeline events never pollute chat history (and vice-versa).
- The `coordinator_graph` keeps the **document** and **unknown** lanes for the file-upload path
  (`process_file_event`). Its `question` lane no longer carries real traffic (text now bypasses the
  graph); it is repointed to the help lane as a defensive fallback.

This matches the master plan's own §4 model, which already depicts chat and document as **two
surfaces over one shared knowledge + tools** — the unification is shared tools/state/Firestore, not a
shared graph.

## Consequences

- Multi-turn chat works the ADK-blessed way; no fragile in-graph hacks.
- Two runners exist: the document/coordinator runner and the chat runner. The Slack layer owns both.
- Chat and document histories are isolated by session id.
- Text turns no longer get the coordinator's `document/question/unknown` classification; the chat
  agent handles off-topic input itself (acceptable — text-with-no-file is already known to be chat at
  the Slack layer; files are detected separately via `file_shared`).
- Profile + ledger context is injected into the chat session via `state_delta` each turn (as today),
  so the agent "knows the client".

## Slack Bolt boundary (2026-06-16 addendum)

The Slack layer must **not** pre-empt the chat assistant with substring keyword gates or canned
replies for conversational turns. File drops vs text is the only deterministic routing at the Bolt
layer; intent (upload help, extraction questions, ledger lookups) is handled by the root
``LlmAgent`` + tools. Real policy guardrails (e.g. un-onboarded channel asking ledger questions)
belong in ADK ``before_model_callback``, not Bolt ``if any(w in text)`` checks.

## Diagnostic state injection (2026-06-16 addendum)

The chat runner (``answer_question``) writes the following diagnostic state keys into every turn's
``state_delta`` so the assistant's tools can introspect without doing their own I/O:

- ``ledger_data`` (existing), ``fy_loaded``, ``ledger_row_count``, ``fy_pointers`` — chosen by
  ``best_fy_for_chat`` (picks the FY whose workbook has the most data, not the highest FY label;
  see ``accounting_agents/ledger_store.py``).
- ``processing_log`` (existing) — recent deliveries from ``client_store.list_processing_log``;
  the runner lazily backfills from ADK doc sessions when empty
  (see ``scripts/backfill_processing_log.py`` for the one-shot equivalent).
- ``processing_log_count``, ``pending_review_count`` — flat counts for ADK instruction
  templating (``{+processing_log_count?+}`` / ``{+pending_review_count?+}`` in
  ``_BASE_INSTRUCTION``); derived from the list keys above.
- ``pending_reviews`` — ``hitl.list_pending_interrupts`` filtered to the current channel.
- ``document_sessions`` — read-only snapshots of per-document ADK session state, keyed by
  ``file_id``; the chat can cite the extraction path / review reasons / source filename without
  doing its own session I/O.

The four new introspection tools (``diagnose_assistant_context``,
``get_document_processing_detail``, ``list_processing_history``, ``list_pending_reviews``) read
these state keys only. They are pure function tools; the runner owns the data.

## Instruction templating + model tier (2026-06-17 addendum)

- ``assistant_agent.instruction`` is a plain string (``_BASE_INSTRUCTION``) with ADK
  ``{+state_key+}`` / ``{+key?+}`` placeholders; the runner's ``state_delta`` fills the
  preamble at LLM call time (no Python ``InstructionProvider`` at runtime).
- Chat lane uses ``config.MODEL_CHAT`` (defaults to ``MODEL_STD`` / gemini-2.5-flash) because
  the 22-tool surface needs reliable multi-step orchestration; override via ``LEDGR_MODEL_CHAT``.
- P1 diagnostic tools live in ``accounting_agents/assistant_tools/introspect.py``; the
  ``LlmAgent`` definition remains ~30 lines at the bottom of ``assistant.py``.

## Addendum (2026-06-17): Thread delivery context + chat UX

When a user replies **in the thread under a batch delivery card** (ADR-0007 job
summary), the Slack runner resolves thread-scoped delivery metadata and injects
it into the chat ``state_delta`` before each ADK turn:

- ``thread_delivery_message_ts`` — parent delivery message ts (= ``raw_thread_ts``)
- ``thread_delivery_filenames`` / ``thread_delivery_invoice_ids`` / ``thread_delivery_fy``
- ``thread_scoped_processing_log`` — processing-log entries whose
  ``delivery_message_ts`` matches the thread parent

Processing-log entries are written per document (including batch mode via
``defer_slack_delivery``) with ``delivery_message_ts`` and ``channel_id`` so the
filter is stable across restarts. Older deliveries without the field fall back to
``conversations.replies`` block parsing.

**Chat UX (Phase 4):** the chat lane mirrors document-upload responsiveness:

- ``reactions.add`` 👀 on the user's message at turn start
- ``assistant.threads.setStatus`` with rotating ``loading_messages`` during ADK
  ``run_async`` (tool-aware labels on function-call events)
- Optional ✅ on the user message after a successful reply
- Error path clears thinking status and removes 👀

Optional **Thinking Steps streaming** (``chat.startStream`` / ``LEDGR_CHAT_STREAM=1``)
is deferred; tool-step streaming requires slack-sdk ≥ 3.40 and is not enabled by
default.

## Alternatives rejected

- **In-graph `mode='chat'`** — impossible; crashes graph validation (see Context).
- **Keep `single_turn`, ship rename+tools only, defer multi-turn** — does not meet Step 1's
  "remembers the thread" goal; carries the per-question-session workaround forward.
- **Chat agent on a START edge in a separate mini-graph** — still needs its own app+runner for the
  text path, with messier session handling than a plain root agent. No benefit.
