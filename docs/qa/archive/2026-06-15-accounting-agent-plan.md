> **⚠️ SUPERSEDED — merged into [`2026-06-15-ledgr-intelligent-agent-masterplan.md`](2026-06-15-ledgr-intelligent-agent-masterplan.md).**
> This was the chat-side half. The master plan unifies it with the engine smart-edges plan into
> one roadmap. Kept for detail/reference only; follow the master plan.

# Plan: Reshape Q&A as a general Accounting Agent (engine-as-tools)

**Status:** planning, no code yet. Grounded in `adk-docs` MCP queries (see §10 for citations).
This v2 replaces two patterns I had hand-rolled in v1 with native ADK primitives.

## 1. The problem in one paragraph

Today, what happens in chat is determined entirely by the **pipeline**. A file upload runs the
deterministic graph (`classify → extract → … → deliver`). A text message routes to `qa_agent`
(single-turn, read-only, 4 tools). A casual reply in a thread saying "actually, change the
Sep 5 row to Office" is treated as a question, has no matching tool, and gets a polite punt.
When extraction misses something on a new vendor format, the only way to fix it is to manually
tune `invoice_processing/extract/...`.

We want the engine to remain the workhorse for cold uploads, **but the agent to become the brain
in chat** — context-aware, multi-turn, able to invoke individual engine capabilities as tools when
the user asks. New behaviour (chat-amend, re-extract with hints, learn from chat, explain a
categorization) is then **tools the agent calls**, not new graph nodes.

## 2. What the agent needs to "know" (already in Firestore)

`invoice_processing/export/client_context.ClientContext.to_state()` already returns the right
shape — chat just needs to seed it on every turn the way the document lane does. Fields available:

- **Identity:** `client_id`, `client_name`, `client_uen`, `region`, `base_currency`, `slack_team_id`.
- **Business shape:** `accounting_software` ("QBS Ledger" / "Xero"), `tax_registered` (GST/SST),
  `fye_month`.
- **Chart of Accounts:** the full `coa` (codes + descriptions + nature).
- **Learned mappings:** `category_mapping` (vendor → account_code) and `entity_memory` (vendor →
  tax_code / role). ADR-0004's durable record of what this client teaches us.
- **Recent work:** ledger pointers (`clients/{id}/ledgers/{fy}` — `slack_file_id`, `seen_doc_keys`)
  and the in-thread context (which document this thread is about, if any).
- **Model identity (transparency):** which Gemini model is replying — exposed via a `model_info()`
  tool so "what model am I talking to?" has a real answer.

ADR continuity: this is just richer use of ADR-0006 (per-client COA onboarding) and ADR-0004
(structured corrections, not memory-bank). Nothing new to store.

## 3. ADK-grounded architecture (rev for v2)

After querying the ADK docs, three things changed from v1:

| Concern | v1 (hand-rolled) | v2 (ADK-native) |
|---|---|---|
| **Write-tool confirmation** | Invent a `confirmation_id` Firestore TTL doc + custom button card | ADK's **`FunctionTool(require_confirmation=True)`** OR `tool_context.request_confirmation(hint, payload)` — runtime pauses the tool, requests human input, resumes with the response |
| **Multi-turn memory in chat** | Add a per-thread session id + custom history capture | LlmAgent's **default** is multi-turn (sees session history). The ONLY change is to drop `mode="single_turn"` (`qa_agent.py:406`) and route chat with `session_id = thread_ts` |
| **Routed sub-agents** | Considered TypeScript `RoutedAgent` | Not in Python ADK. Use one LlmAgent + LLM-driven tool selection, or coordinator + sub-agents (what we have). Per-tool model override is the right knob. |

**Recommended shape (stays C from v1, now grounded):**

```
                       ┌────────────────────────────────────────────────┐
upload  ──► coordinator ── document ──► engine pipeline (unchanged)
                       │
text    ──►            ── chat     ──► accounting_agent (multi-turn LlmAgent)
                       │                    │
                       │                    ├── READ tools (no confirm)
                       │                    ├── WRITE tools — require_confirmation=True
                       │                    └── REASON tools (explain, model_info)
                       └── unknown ──► fallback ("upload a file or ask me about your books")
```

The engine pipeline stays the system-of-record path (ADR-0001/0003). The chat agent never
re-implements it; tools are thin wrappers around the same `SlackLedgerStore`, `_categorize_*`,
`_apply_tax_*` functions the nodes use.

## 4. Tool inventory — read / reason / write

Naming: `verb_noun`, lowercase, JSON return. Where ADK natives apply, I cite the doc.

### Read tools

| Tool | Purpose | Status |
|---|---|---|
| `bank_totals(month?, year?)` | withdrawals/deposits/net/opening/closing | ✅ shipped (F1) |
| `summarize_by_category()` / `pnl_for_fy()` / `gst_threshold_check()` | existing | ✅ |
| `lookup_row(month?, vendor?, description?)` | find a specific transaction | ➕ |
| `list_recent_documents(limit?)` | "what did I upload this week?" | ➕ |
| `show_client_profile()` | client name + FYE + software + GST + COA size | ➕ |
| `show_learned_mappings(vendor?)` | what's in `category_mapping` / `entity_memory` | ➕ |
| `model_info()` | resolved Gemini model id (transparency) | ➕ |

### Reason / explain tools

- `explain_categorization(month, vendor)` — re-runs `categorize_node` logic on the matched row and
  exposes which rule fired (learned mapping / keyword / fallback).
- `explain_tax_treatment(month, vendor)` — re-runs `tax_node` logic.
- `summarize_recent_activity(days=7)` — plain-English brief: posted N invoices / M bank
  statements; biggest categories X/Y; rows needing review.

### Write tools — all use ADK's native confirmation

Each is a `FunctionTool(..., require_confirmation=True)` OR uses `tool_context.request_confirmation(
hint="Apply this change?", payload={...preview...})` for richer previews. The runtime suspends the
tool, communicates the request to Slack, and resumes with the response. We don't invent a TTL doc.

| Tool | What it does |
|---|---|
| `amend_ledger_row(month, match, set)` | edit account_code / tax / description / amount of a posted row; re-runs `_recompute_balances` + `rebuild_account_sheet` and re-uploads |
| `remove_ledger_row(month, match)` | delete a row; same re-upload path |
| `re_extract_document(file_id, hints?)` | re-run extraction with hints ("this is a credit note", "GST is 9% inclusive"); result enters the engine at post-extraction |
| `replace_recorded_month(month)` | the "re-process this" path from F6 — clears a single `seen_doc_keys` entry so a re-upload appends fresh |
| `learn_mapping(vendor, account_code, tax_code?)` | append to `category_mapping` / `entity_memory` (ADR-0004 path) |

### Engine-call tools (the part that makes the agent flexible)

Wrap the pure parts of each engine node as a tool the agent can call on already-loaded data:

| Engine node | Tool wrapper |
|---|---|
| `classify_node` | `classify_uploaded_doc(file_id)` |
| `extract_invoice_node` | `extract_invoice(file_id, hints?)` |
| `extract_bank_node` | `extract_bank_statement(file_id, hints?)` |
| `categorize_node` | inside `explain_categorization` / `amend_ledger_row` |
| `tax_node` | inside `explain_tax_treatment` |

The agent never re-implements these — tools are thin wrappers around existing functions.

## 5. Safety — using ADK's confirmation primitive

Two ADK options (both apply here; advanced is richer):

**Boolean confirmation** — `FunctionTool(amend_ledger_row, require_confirmation=True)`.
Simplest. The runtime asks Yes/No; the tool body only runs after Yes.

**Advanced confirmation** — `tool_context.request_confirmation(hint="Change Sep 5 / FAST PAYMENT
from Travel→Office?", payload={"row_key": "...", "current": {...}, "proposed": {...}})`. We get
the preview shown to the user and the user's payload back, so the tool can apply EXACTLY the
preview it announced. Best for amend/remove.

How the pause is delivered to Slack: ADK emits an `adk_request_confirmation` function-call event
(same family as the `adk_request_input` interrupts we already handle in `app/blocks.py`'s approval
card). The runner posts a card; the button click POSTs back a `function_response`. The same
button block builder we use for HITL Approve/Edit/Reject is reused.

**Idempotency caveat (cited in Resume docs):** tool execution may happen more than once during
resume. Every write tool keys on the resolved `confirmation_id` + `set` payload and is a no-op if
the change is already present in the workbook. We already have this discipline in
`tests/test_resume_idempotency.py`; extend it to the new write tools.

**Known ADK limitation we must check:** `Tool Confirmation` is documented as **not supported by
DatabaseSessionService or VertexAiSessionService**. We use a custom `FirestoreSessionService`
(`accounting_agents/sessions.py`); this needs a smoke-test before we depend on it. Fallback: use
the existing `adk_request_input` interrupt path (same shape, already proven in our HITL).

## 6. Sessions, state, model selection — ADK-native

- **Sessions = threads.** ADK `SessionService` keys interactions by `session_id`. Use
  `session_id = thread_ts` (or `channel_id` for a top-level message) so the agent recalls the
  prior 2–3 turns in this thread.
- **State seeding per turn.** Continue calling `_profile_state_delta(client_store, channel_id)`
  (already exists) so every chat turn has `client_id`/`client_name`/`fye_month`/`coa`/
  `category_mapping`/`entity_memory` in `session.state`. Tools read it via `tool_context.state`.
- **Multi-turn = default mode.** Drop `mode="single_turn"` from `qa_agent` definition. ADK then
  passes the session's prior events as context (per LlmAgent docs). One line.
- **Model tiering per tool.** Keep the chat agent on `MODEL_LITE`. For `re_extract_document`,
  set `model=MODEL_STD` (or `MODEL_PRO`) on that specific tool — aligns with the
  `model-tiering-and-no-a2a` memory.
- **No ADK Memory service needed.** ADK's `MemoryService` is for unstructured cross-session
  recall. Our learning lives in Firestore (`category_mapping`, `entity_memory`) per ADR-0004 —
  surface it as tool reads, don't move it.

## 7. Skills as an optional packaging shape (ADK v1.25.0+)

ADK now has a **Skills** primitive: each skill is a folder `SKILL.md` (frontmatter + body) +
`references/` + `assets/` + `scripts/`, loaded on demand via `SkillToolset`. L1 metadata is always
visible to the agent; L2 instructions load only when the skill is triggered; L3 resources load as
needed. Designed exactly to fight context-window bloat when you have many tools.

Two ways to use this:

- **Phase 0 / 1:** one `accounting_agent` with all tools registered directly (simpler).
- **Phase 2+:** as the tool count grows (amend, re-extract, learn, explain, etc.), repackage each
  cluster as a skill: `skills/amend-ledger/`, `skills/re-extract/`, `skills/explain/`,
  `skills/learn/`. Each skill carries its OWN instructions and OWN tool subset, loaded on demand.

I recommend **deferring Skills to Phase 3** — it's experimental, adds project complexity, and the
direct-tools path lets us learn what tools actually need to exist before we package them.

## 8. Phasing — small commits, ship at each phase

**Phase 0 — rename + multi-turn + 3 small read tools (NO writes yet)**
- Rename `qa_agent` → `accounting_agent` (file kept, symbol renamed); update `accounting_agents/
  __init__.py` re-exports if any.
- Drop `mode="single_turn"` (`qa_agent.py:406`) → multi-turn.
- Switch chat session id `_per_question_session_id(channel, ts)` → use `thread_ts` (or
  `channel_id` for top-level) so the agent recalls prior turns.
- Add `show_client_profile`, `show_learned_mappings`, `model_info`. Live-verify on Sample Bank Client.

**Phase 1 — explain + lookup (read-only)**
- `explain_categorization`, `explain_tax_treatment`, `summarize_recent_activity`,
  `list_recent_documents`, `lookup_row`. All pure / read. Live-verify on Sample Bank Client history.

**Phase 2 — write gate + amend (uses ADK's native confirmation)**
- Smoke-test ADK confirmation on our `FirestoreSessionService` first (the documented limitation).
  Fallback to `adk_request_input` interrupt if needed — same blocks pattern.
- `amend_ledger_row` + `remove_ledger_row` behind `require_confirmation=True` with
  `tool_context.request_confirmation(hint=…, payload=…)`. Audit log every applied write.
- New ADR (0008): "chat-amend posted ledger via ADK tool confirmation".

**Phase 3 — re-extract & learn (writes that touch extraction + learning)**
- `re_extract_document(file_id, hints)` and `replace_recorded_month(month)`.
- `learn_mapping(vendor, …)` — writes to `category_mapping` / `entity_memory`, gated.
- Consider repackaging tool clusters as **Skills** to keep context lean.

**Phase 4 — smarter extraction without manual tuning**
- After Phase 3 ships, the agent can inspect a low-confidence extracted field and **propose** a
  hint-driven re-extract automatically (with confirmation). This is the "smart, knows what to do"
  behaviour built on top of Phases 0–3, not before them.

## 9. Open decisions — ADK-aware answers

For each decision, I now know what ADK actually offers, so I've sharpened the recommendation.

| # | Question | Recommendation (ADK-aware) |
|---|---|---|
| 1 | Multi-turn memory scope: thread or channel? | **Thread.** `session_id = thread_ts or channel_id`. Native to `SessionService`. |
| 2 | Write-confirmation primitive: hand-rolled or ADK-native? | **ADK `request_confirmation`** with payload (so the tool applies exactly what was shown). Fall back to `adk_request_input` if `FirestoreSessionService` incompatibility surfaces — same blocks UX. |
| 3 | Who can confirm a write? | **Triggering user only.** Enforce in the button handler (`user_id` check), not in ADK — ADK doesn't gate it. |
| 4 | Model strategy | **Chat on `MODEL_LITE`; `re_extract_document` overrides to `MODEL_STD`.** Per-tool override is supported. |
| 5 | Rename `qa_agent` → `accounting_agent`? | **Yes.** Internal only; no external contract breaks. |
| 6 | Skills now or later? | **Later (Phase 3).** Experimental + we don't know the right cluster boundaries yet. |
| 7 | Memory service for cross-session recall? | **No.** Our learning is structured (Firestore per ADR-0004); ADK Memory targets unstructured recall. |
| 8 | Routed sub-agents? | **No (Python doesn't have `RoutedAgent`).** One agent, tools differ by need; coordinator stays as today. |

## 10. ADK doc citations (queried via `adk-docs` MCP, 2026-06-15)

- **Tool Confirmation** (`adk.dev/tools-custom/confirmation/`):
  - `FunctionTool(require_confirmation=True|callable)` for boolean.
  - `tool_context.request_confirmation(hint, payload)` for advanced (preview + structured response).
  - Pauses tool execution; user response arrives as `function_response` with name
    `adk_request_confirmation`.
  - **Known limitations:** `DatabaseSessionService` and `VertexAiSessionService` NOT supported.
    Behaviour under Resume changes — must include `invocation_id` in the confirmation response.

- **Sessions / State / Memory** (`adk.dev/sessions/`):
  - `Session` = one conversation thread (events + state). `SessionService` manages lifecycle.
  - `State` = per-session, NOT cross-session.
  - `MemoryService` = cross-session searchable knowledge (separate concern).
  - In-memory implementations are for dev only; persistence requires a configured service.

- **Resume** (`adk.dev/runtime/resume/`):
  - `App(... resumability_config=ResumabilityConfig(is_resumable=True))` — already in our `agent.py:254`.
  - On resume, tools may execute MORE THAN ONCE. Write tools must check for duplicate runs.
  - Confirmations + Resume: include `invocation_id` in the confirmation response, otherwise the
    runtime starts a fresh invocation.

- **Skills** (`adk.dev/skills/`):
  - Python v1.25.0+, experimental. Folder structure (`SKILL.md` + `references/` + `assets/` +
    `scripts/`) — loaded incrementally to minimise context window impact.
  - `SkillToolset(skills=[...], additional_tools=[...])` plugs into `tools=[…]`.
  - L1 metadata always visible; L2 instructions on trigger; L3 resources on demand.

- **LlmAgent** (`adk.dev/agents/llm-agents/`):
  - `name`, `description`, `model`, `instruction` (string or provider), `tools`, `sub_agents`.
  - Description matters for multi-agent delegation; LLM uses it to pick a sub-agent.
  - Our existing `mode="single_turn"` forces `include_contents='none'`; default mode passes history.

- **Routing (Python)** — `adk.dev/agents/routing/`:
  - `RoutedAgent` is **TypeScript only** today. Python uses coordinator + sub-agents.
  - Per-model routing is available via `RoutedLlm`.

- **Human input nodes (graphs)** — `adk.dev/graphs/human-input/`:
  - `RequestInput(message=, payload=, response_schema=)` for graph-based HITL.
  - Different primitive from tool-level `request_confirmation` — we use both: graph nodes for
    document HITL (existing), tool confirmation for chat-amend (new).

## 11. Anti-goals (unchanged)

- Not a rewrite of the deterministic engine. Cold uploads still flow through the pipeline; ADR-0001 stays.
- Not a free-form "agent does whatever it wants" loop. Tools are explicit; writes are gated by ADK.
- Not a place to keep adding nodes. New behaviour lives as tools the agent can call.
- Not a replacement for HITL on cold uploads. The pre-post Approve/Edit card stays exactly as it is.

---

## Asks for the user — same five as before, ADK-clarified

1. Confirm the shape (one `accounting_agent`, tool-level `require_confirmation`, engine-as-tools).
2. Confirm the **8 decisions in §9** (or override any).
3. Confirm the phasing order. Happy to swap Phase 1 ↔ Phase 2 if chat-amend is the more urgent UX
   win — they touch different files.

Once those are answered, I'll start Phase 0 (rename + multi-turn + 3 small read tools) on a fresh
branch, run the FirestoreSessionService × tool-confirmation smoke test, and report back before
touching writes.
