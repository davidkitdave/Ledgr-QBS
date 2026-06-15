# Ledgr master plan: from a processing engine to an intelligent accounting agent

**This is the single source of truth.** It merges and supersedes:
- `2026-06-15-accounting-agent-plan.md` (the chat side)
- `2026-06-15-engine-smart-edges-plan.md` (the engine side)

Those two were never two ideas — they are the two halves of one brain. This doc unifies them.
Grounded in `adk-docs` MCP research (citations in §8). Status: planning. Build happens in a later
session, phase by phase.

---

## 0. The one-paragraph vision

When a firm onboards to Ledgr in Slack, they are not buying a document-processing machine. They
are **hiring an intelligent junior accountant** that happens to use a very fast machine when it's
handy. The accountant knows the client (name, tax status, chart of accounts, history), checks its
own work, asks before doing anything risky, reasons about cases it hasn't seen before, and can be
talked to in chat to actually *do* things — not just answer questions. The deterministic engine
we already built becomes this accountant's fastest tool, not the whole product.

## 1. The simple mental model (so we never lose the thread)

Today: a **conveyor belt of dumb robots**. Paper in → read → categorize → tax → write to notebook
→ out. No robot checks the one before it. Mistakes are only caught at the very end. A separate,
hand-tied helper answers simple questions but can't act.

Target: the **same belt, but with a smart inspector between each station**, plus the helper grown
into a real assistant that can use every tool on the belt. The inspector does one of three things
at each seam: wave it through, send it back for a retry with a hint, or ask the human a precise
question *right now*.

The product promise: **"You hired a bookkeeper, not a machine."**

## 2. Why accounting demands this (why hard-coding fails)

Accounting is judgement under variation. Every client has a different chart of accounts; every
vendor formats invoices differently; receipts are blurry, multi-currency, multi-page, credit
notes, deposits, dividends. A deterministic rule engine can cover the common 80–90%, but the long
tail is effectively infinite — you cannot write a rule for every case, and trying to (the band-aid
treadmill) never ends. The intelligence has to live in the *edges* (review, retry, ask, reason),
backed by the deterministic engine for speed and auditability on the common path.

Evidence from our own work: every fix shipped on this branch — `N()` coercion in Math_Check,
`_is_formula_or_missing`, content-based `doc_key`, block-level dedup — is a band-aid for a long-tail
case the deterministic edge couldn't reason about. A self-reviewing edge would have caught most of
them before they reached the workbook.

## 3. What we have today (honest inventory)

**Works well (keep):**
- Deterministic engine for the happy path: `classify → extract → categorize → tax → approval_gate
  → route → consolidate → deliver` (ADR-0001). Fast, cheap, auditable.
- End-of-pipeline HITL Approve / Edit / Reject (ADR-0007).
- Structured learning: `category_mapping` + `entity_memory` per client (ADR-0004).
- Per-channel client profile in Firestore: name, UEN, region, currency, GST status, FYE month,
  full COA (ADR-0006).
- This branch's 12 shipped UX/data fixes (dedup, bank-vs-ledger wording, client-scoped filenames,
  bank Q&A tool, warmer status voice, Sept-dup cleanup).

**Structurally limiting (fix):**
1. Pipeline never checks its own work — bad extraction flows straight through.
2. HITL only fires at the END — user can't intervene mid-flow.
3. Edge cases handled by accretion (band-aids), not intelligence.
4. The chat helper (`qa_agent`) is a separate, smaller brain: 4 read-only tools, no engine access.
5. Categorization is keyword-match + fallback — cannot reason about new vendors.
6. Tax determination is rules + fallback — same limitation.
7. The router is the dumbest possible edge (intent ∈ {document, question, unknown}).

## 4. The unified architecture — one brain, two surfaces

```
                         ┌──────────────── ONE SHARED KNOWLEDGE ────────────────┐
                         │  Firestore: client profile, COA, category_mapping,    │
                         │  entity_memory, ledger pointers (per FY)              │
                         └───────────────────────────────────────────────────────┘
                                    ▲                          ▲
            ┌───────────────────────┘                          └───────────────────────┐
   SURFACE A: DOCUMENT LANE (engine + smart edges)     SURFACE B: CHAT LANE (the assistant)
   upload ─► classify ─►[inspector]─► extract ─►        text ─► accounting_agent (multi-turn)
            [inspector]─► categorize ─►[inspector]─►            │
            tax ─► route ─► consolidate ─► deliver             ├─ READ tools  (no confirm)
                    │                                           ├─ WRITE tools (ADK confirmation)
            inspectors can: pass · retry-with-hint ·            └─ REASON tools (explain, model_info)
            ask-user (mid-flow HITL)                            │
                                                    BOTH lanes call the SAME node implementations
                                                    (extract, categorize, tax) and the SAME tools.
```

The crucial unification: **the document lane and the chat lane are the same agent using the same
tools over the same knowledge.** When the engine's inspector decides "re-extract with a hint," it
calls the exact same `extract_node` that the chat agent's `re_extract_document(file_id, hints)`
tool calls. When the user in chat asks "why did this go to Office?", the `explain_categorization`
tool re-runs the exact same categorizer the pipeline used. One mind, two doors into it.

## 5. The ADK primitives we'll build on (all verified via MCP)

| Need | ADK primitive | Doc |
|---|---|---|
| Smart edges (review, loop, branch, mid-flow HITL) with auto-resume | **Dynamic Workflows** (`@node` + `ctx.run_node` + Python control flow + checkpointing) | graphs/dynamic |
| Inspector between two nodes | **Generate-and-Review** (generator → critic → branch on status) | workflows/patterns |
| "Retry until good enough" | **Iterative Refinement** (`LoopAgent` + `escalate=True`) | workflows/patterns |
| Ask the user before a risky write (chat OR mid-pipeline) | **Tool Confirmation** (`FunctionTool(require_confirmation=True)` / `tool_context.request_confirmation(hint, payload)`) | tools-custom/confirmation |
| Multi-turn chat that remembers the thread | LlmAgent default mode (drop `mode="single_turn"`); `session_id = thread_ts` | sessions; llm-agents |
| Route work to a specialist | Coordinator + `sub_agents` (LLM-driven delegation) — already used at top level | workflows/patterns |
| Modular tool packaging when the toolset grows | **Skills** (`SkillToolset`, experimental v1.25.0+) | skills |

Deliberately **not** using: ADK `MemoryService` (our learning is structured in Firestore, keep it);
Python `RoutedAgent` (TS-only today); `BaseAgent` custom orchestration (Dynamic Workflows supersede
it). Known caveat to test early: Tool Confirmation is documented as unsupported on
DatabaseSessionService / VertexAiSessionService — must smoke-test our custom FirestoreSessionService
first; fallback is the `adk_request_input` interrupt we already use in HITL.

## 6. What we're missing (the reverse-thinking gap analysis)

Start from "a real junior accountant joined the firm." What would they do that we can't?

| A real accountant… | Today | The capability to build |
|---|---|---|
| Re-reads a blurry doc if the first read looked off | reads once | **Extract reviewer + retry-with-hints** (E-Move 1) |
| Asks "is this a credit note?" BEFORE filing | asks only at the end | **Mid-flow HITL** via `RequestInput`/confirmation |
| Reasons about a brand-new vendor from the COA | keyword/default | **Hybrid categorizer** (LLM on no-match) (E-Move 2) |
| Fixes a row when you point it out in chat | chat can't act | **Write tools, gated** (amend/remove) (C-Phase 2) |
| Remembers a rule you said once in chat | learns only from formal Edit | **learn_mapping from chat** (C-Phase 3) |
| Knows the client cold | engine knows; chat mostly doesn't | **Profile-seeded chat agent** (C-Phase 0) |
| Tells you which categories are biggest, what needs review | limited Q&A | **Explain/summary tools** (C-Phase 1) |
| Proactively flags "this one's odd, want me to redo it?" | nothing | **Proactive auto-hints** (C-Phase 4) |

## 7. The unified roadmap (one ordered list, not two)

Each step is independently committable, behind tests, and live-verifiable. C = chat surface,
E = engine surface — but they share code, so they interleave on purpose.

| # | Step | Surface | Outcome |
|---|---|---|---|
| 1 | Rename `qa_agent` → `accounting_agent`; drop `single_turn` (multi-turn); seed client profile into chat; add 3 read tools (`show_client_profile`, `show_learned_mappings`, `model_info`) | C-0 | The chat helper knows who the client is and remembers the thread |
| 2 | **Extract reviewer + retry-with-hints** between extract and categorize (Generate-and-Review / small LoopAgent). Verdict `{ok / hints_needed / user_clarify}`; mid-flow HITL on `user_clarify` | E-1 | The engine checks its own work — the single highest-leverage move |
| 3 | Explain + lookup read tools (`explain_categorization`, `explain_tax_treatment`, `summarize_recent_activity`, `lookup_row`, `list_recent_documents`) — reuse the engine's own categorizer/tax logic | C-1 | The assistant can explain *why*, grounded in the same engine |
| 4 | Write gate + `amend_ledger_row` / `remove_ledger_row` via ADK Tool Confirmation (smoke-test Firestore session first; fallback `adk_request_input`). Audit every write. ADR-0008. | C-2 | The assistant gets hands — can fix the book, safely, with one-click confirm |
| 5 | **Hybrid categorizer** — fast path (learned → keyword) unchanged; on no-match, a small LlmAgent reasons from `{line, COA, entity_memory}` → `{account_code, why}`; result feeds learning | E-2 | New vendors get reasoned, not defaulted — ends the keyword treadmill |
| 6 | Migrate document-lane orchestration to **Dynamic Workflows** (`@node` + `ctx.run_node`), now that the smart-edge shape is proven | E-3 | Clean, resumable, easy to add the next inspector per node |
| 7 | `re_extract_document(file_id, hints)` + `replace_recorded_month` + `learn_mapping` from chat — both lanes now share the exact same engine tools | C-3 | Talk-to-it-and-it-acts; the engine and chat are one mind |
| 8 | Proactive auto-hints: a low-confidence reviewer verdict prompts the agent to *offer* a redo ("this one looks off — want me to re-extract as a credit note?") | C-4/E | The "smart, knows what to do" end state, built on everything above |

Recommended starting pair: **Step 1 then Step 2.** Step 1 is the smallest unblock; Step 2 is the
biggest single intelligence win on the engine.

## 8. ADK citations (queried 2026-06-15 via `adk-docs` MCP)

- **workflows/patterns** — Coordinator+dispatcher, Sequential, Parallel, Hierarchical,
  Generate-and-Review, Iterative Refinement (LoopAgent + `escalate=True`), HITL (tool-based +
  PolicyEngine).
- **graphs/dynamic** (Python v2.0.0) — `@node`, `ctx.run_node`, loops/conditionals, automatic
  checkpointing; parent nodes need `rerun_on_resume=True`.
- **graphs** (v2.0.0) — graph workflows; nodes can be agents/tools/code/sub-workflows; not
  compatible with Live Streaming.
- **tools-custom/confirmation** — `require_confirmation` boolean/callable; `request_confirmation(
  hint, payload)` advanced; pauses tool, resumes on `function_response`; NOT supported on
  Database/VertexAi session services; needs `invocation_id` under Resume.
- **sessions** — Session = one thread (events+state); State = per-session; Memory = cross-session
  (we don't need it).
- **llm-agents** — `name/description/model/instruction/tools/sub_agents`; `description` drives
  delegation; our `mode="single_turn"` forces `include_contents='none'` (drop it for multi-turn).
- **skills** (experimental v1.25.0+) — `SkillToolset`; SKILL.md + references/assets/scripts;
  loaded incrementally to save context. Use in Step 6+ if the toolset grows large.
- **agents/routing** — `RoutedAgent` is TypeScript-only today; Python uses coordinator+sub_agents.
- **graphs/human-input** — `RequestInput(message, payload, response_schema)` for graph HITL nodes.
- **evaluate** — trajectory + output eval; `tool_trajectory_avg_score` (catches wandering),
  `final_response_match_v2`, `hallucinations_v1`, `multi_turn_trajectory_quality_v1`; `.test.json`
  /`.evalset.json`; run via `pytest` (`AgentEvaluator.evaluate`), `adk eval` CLI, or `adk web`
  Trace view. (Backs §10.)

## 9. Cost & reliability guardrails (why this is NOT a token-burning random-walker)

A prior exploration used a single autonomous LLM agent in an open reasoning loop. It wandered on
hard invoices and burned tokens because **the LLM was driving the control loop with no bounds**.
This design removes that failure mode by construction. The mechanisms, explicitly:

1. **Code orchestrates; the LLM is a tool, not the driver.** Order of steps is decided by a
   deterministic `@node` function (or the existing graph wiring), not by an LLM choosing its next
   move. An LLM that doesn't drive the loop cannot wander the loop. (Ref: `agent-pattern-
   deterministic-spine`.)
2. **The happy path spends ~zero reasoning.** Clean doc: classify (1 structured call) → extract
   (1 structured call) → categorize (learned-map / keyword = 0 LLM) → tax (rules = 0 LLM) → write.
   That is essentially today's cost. Smart edges only spend on genuinely hard docs.
3. **Every smart edge is hard-capped.** Reviewer/refiner loops use `LoopAgent(max_iterations=2)` +
   `escalate=True`. Worst case for a hard doc = today + ~2 small reviewer calls + ≤1 re-extract.
   A known, bounded ceiling — never open-ended.
4. **Cheapest model, tightest prompt.** Reviewers + hybrid categorizer run on `MODEL_LITE`
   (flash-lite). Inputs are narrow (one line + COA + entity_memory), not whole documents.
5. **Circuit breaker → human.** Trip the retry ceiling → stop spending, post a HITL card. A human
   answer is far cheaper than agent thrashing. Cost has a hard stop, always.
6. **One app, one coordinator, no A2A.** No agent-to-agent chatter, no parallel ADKs. (Ref:
   `model-tiering-and-no-a2a`.)
7. **Token budget is a tracked metric, not a hope.** Per-document and per-chat-turn token spend is
   measured and gated in eval (see §10). A regression that increases spend fails the gate.

Net: per-document cost on the common path stays ≈ today; the marginal cost of intelligence is a
small, bounded, measured number that only the long-tail docs incur — and it is provably bounded,
not merely intended to be.

## 10. Verification & QA plan (runs AFTER each build phase — a quality gate)

Each roadmap step (§7) is followed by a verification gate before the next step starts. Grounded in
ADK's eval framework (`adk.dev/evaluate/`), which exists precisely because LLM behaviour needs
trajectory + output checks, not just pass/fail unit tests.

**A. Golden eval sets (the ground truth we already have).**
- Build `.test.json` / `.evalset.json` from the Cast Unity test docs + their ground truth, and the
  Rosebery reference workbook. Cover: clean invoice, clean bank statement, AND the adversarial
  long-tail that broke us before (credit note, multi-currency, multi-invoice PDF, blurry scan,
  missing balance).
- Run via `pytest` (`AgentEvaluator.evaluate`) in CI and `adk eval` from the CLI.

**B. The anti-random-walk metric (the direct answer to the cost worry).**
- `tool_trajectory_avg_score` (target 1.0 on the engine path) — asserts the agent/engine took the
  EXPECTED sequence of tool calls. If it wanders or adds spurious steps, the gate FAILS. This is
  the mechanism that turns "random walking" into a caught regression instead of a surprise bill.
- `multi_turn_trajectory_quality_v1` — for chat, scores the efficiency/logic of the conversation
  steps (did it solve it directly, or thrash?).

**C. Correctness metrics.**
- `final_response_match_v2` / `response_match_score` — answers match the ground-truth values
  (e.g. "Oct withdrawals = SGD 4,221.14").
- `hallucinations_v1` — every figure the agent states must be grounded in a tool output; flags
  any invented number. Critical for accounting.
- Domain assertions (our own, deterministic): categorization accuracy vs ground truth, tax-code
  accuracy, balance/Math_Check correctness, dedup correctness.

**D. Cost regression gate (our own metric on top of ADK).**
- Record tokens-per-document and tokens-per-chat-turn for each eval case. Set a ceiling per case.
  A phase that raises spend beyond budget fails the gate — so "more intelligent" can never quietly
  become "more expensive" without us seeing it.
- Track the happy-path doc separately: it must stay at ≈ today's cost (the smart edges must not
  fire on clean docs).

**E. Regression safety net.**
- The existing 860 unit tests must stay green every phase.
- Live Slack QA session per phase (the computer-use QA we've been running), against real Cast
  Unity docs — the final human check.
- `adk web` Trace view for debugging any eval failure (shows the exact request/response/tool graph
  per step).

**F. Definition of "better" (so the QA has a target).**
- A phase ships only if: trajectory gate passes (no wandering), correctness ≥ the deterministic
  baseline it replaces, hallucinations = 0 on the golden set, cost within budget, 860 tests green,
  and the live QA session confirms it on real docs. "Better" = more correct on the long tail at
  bounded, measured cost — not just "feels smarter".

## 11. Anti-goals (what this plan is NOT)

- **Not** a rewrite of the deterministic engine. The happy path stays fast, cheap, auditable.
- **Not** an LLM in every edge — only where a deterministic rule is failing or accreting band-aids.
  Measure before adding an inspector.
- **Not** a free-form autonomous agent. Every write is gated by ADK confirmation; HITL stays the
  escape valve, now available earlier and from chat.
- **Not** moving structured learning out of Firestore into ADK Memory.
- **Not** breaking the system-of-record contract: `SlackLedgerStore.append_rows` and the Slack-
  hosted FY workbook remain the record.

## 12. Decisions needed before the build session

1. **Buy the unified vision?** One agent, two surfaces (chat + smart-edged engine), shared
   knowledge and tools — not a rewrite, not two separate products.
2. **Start with Step 1 → Step 2?** (rename+multi-turn+profile, then extract reviewer.)
3. **For Step 2, keep the existing graph wiring and slot the reviewer in as a SequentialAgent
   sub-step** (recommended), deferring the Dynamic Workflows migration to Step 6 — or migrate now?
4. **Confirmation primitive:** ADK Tool Confirmation if the Firestore-session smoke test passes,
   else the proven `adk_request_input` path. OK to let the smoke test decide?
5. **Model tiering:** chat + reviewers + hybrid categorizer on `MODEL_LITE`; promote to `MODEL_STD`
   only for re-extraction. OK?

Answer "default to your recommendations" or override any, and the next session starts at Step 1
on a fresh branch.
