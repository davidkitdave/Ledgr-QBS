# Ledgr master plan: from a processing engine to an intelligent accounting agent

**This is the single source of truth.** It merges and supersedes:
- `2026-06-15-accounting-agent-plan.md` (the chat side)
- `2026-06-15-engine-smart-edges-plan.md` (the engine side)

Those two were never two ideas — they are the two halves of one brain. This doc unifies them.
Grounded in `adk-docs` MCP research (citations in §8). Status: **Step 1 BUILT and live-QA'd 2026-06-15**
(see §0.5 corrections). Subsequent steps build in later sessions, phase by phase.

---

## 0. The one-paragraph vision

When a firm onboards to Ledgr in Slack, they are not buying a document-processing machine. They
are **hiring an intelligent junior accountant** that happens to use a very fast machine when it's
handy. The accountant knows the client (name, tax status, chart of accounts, history), checks its
own work, asks before doing anything risky, reasons about cases it hasn't seen before, and can be
talked to in chat to actually *do* things — not just answer questions. The deterministic engine
we already built becomes this accountant's fastest tool, not the whole product.

## 0.5 Step-1 corrections (added 2026-06-15 after live build + Slack QA)

This plan was written before Step 1 was built. Step 1 shipped (rename → `assistant`, multi-turn,
profile-seed, 7 read tools, 879 unit tests, live-QA'd against the developer's local test-firm docs
in a per-sub-client Slack channel). Two small follow-ups also landed (1.5a HITL field-name fix, 1.5c tax classifier
master-gate). The build surfaced FIVE corrections this plan author could not have known about. Read
these alongside the original §3–§11 text below.

**A. Chat lane is a STANDALONE root agent (ADR-0008), not a multi-turn node in the graph.**
The literal "drop `single_turn`" on `qa_agent` (originally proposed in §7 Step 1) was infeasible:
ADK 2.2.0 forbids `mode='chat'` LlmAgents as downstream graph nodes
(`workflow/_graph.py:520-538` `_validate_chat_agent_wiring` raises `ValueError`; docs
`adk.dev/graphs/routes` confirm graph nodes must be `task` or `single_turn`; `task` is disabled in
v2.0.0 graphs). The chat lane now runs on its own `App` + `Runner`, with a per-thread
(`thread_ts`) — or per-UTC-day fallback for channel-root messages — session id, **isolated** from
document-processing sessions (otherwise pipeline events pollute chat history). §4's "two surfaces
over shared knowledge + tools" is exactly this shape: surfaces share Firestore profile + ledger +
tool *code*, but NOT sessions and NOT graphs. See `docs/adr/0008-chat-lane-standalone-root-agent.md`.
The §4 diagram is structurally correct; only the wording about the chat agent being IN the
coordinator graph is wrong.

**B. `gemini-2.5-flash-lite` sporadically goes silent after a tool call — defend it at every tool-heavy lane.**
Caught TWICE during Step 1 live QA. The model emits a final event with no text parts after the tool
returns; user sees a canned "rephrase your question" fallback. Two defences shipped and MUST repeat
for Steps 2/5/7: (i) the instruction must explicitly require a final text reply ("NEVER end your
turn with only a tool call and no text reply"); (ii) the runner needs a safety net that surfaces the
last `function_response.result` when `extract_final_text` is empty
(`accounting_agents/slack_runner.py:extract_tool_response_text`). This is a **reliability concern,
not a cost concern**, but it intersects with §9.5's "circuit breaker → human" — silence is an
accidental circuit-break, and the safety net keeps it from masquerading as "I have no answer". Add
this to §9 as a guardrail. Memory: `gemini-flash-lite-silent-after-tool`.

**C. Singapore GST master gate is OUR-side, applies to BOTH directions, overrides doc content.**
User-confirmed rule (memory `sg-gst-tax-rule-and-xero-codes`): if the Ledgr client is NOT
GST-registered, EVERY purchase AND sales line is `NT` regardless of doc content — input GST becomes
cost, output GST cannot be legally charged. If the client IS registered, the doc dictates the
SR/ZR split. Pre-fix, `tax_classifier.py` purchase branch ignored `inv.our_gst_registered` and
wrongly coded SR on non-reg-client invoices — silently wrong numbers in the books. Fixed in 1.5c
(hoisted master gate above tax_keyword). **Implications for later steps:**
- **Step 2 (extract reviewer)** MUST NOT flag a non-reg client's "missing tax" as a struggle
  signal — `NT` IS the right code for them. The reviewer's "low confidence" should ignore the
  `tax_*` signals entirely when `inv.our_gst_registered is False`.
- **Step 4 (write tools)** MUST re-run the classifier on amended lines so `amend_ledger_row`
  cannot bypass this rule via the chat agent.
- **Step 5 (hybrid categorizer)** MUST seed the LLM prompt with `client.tax_registered` so the
  LLM doesn't fight the gate.

**D. HITL Edit field-name mismatch — and the pattern that produced it.**
Caught and fixed in 1.5a: `EDITABLE_LINE_FIELDS = ("account_code", "tax_code", "amount", ...)` did
not match canonical `InvoiceLine` model fields (`tax_treatment`, `net_amount`), so every Edit
silently no-op'd against the exporter columns. Add to §11 anti-goals: **any DTO that crosses the
chat/Slack/graph/exporter boundaries MUST use the canonical `InvoiceLine` field names**. A
regression test exists at `tests/test_nodes.py::test_apply_decision_node_edit_does_not_silently_drop_canonical_fields`.

**E. The chat agent feels "rigid" through Step 3 — this is BY DESIGN; recommend reordering.**
The user noticed this within minutes of Step 1 going live (asked "fix this row" → agent only has
read tools). Step 3 adds *explain* (why-coded verbs); Step 4 adds *amend* (the "fix it" verb).
**Recommended reorder of §7's roadmap** (vs. the original order):

    Original:   1 → 2 (E-1 reviewer) → 3 (C-1 explain) → 4 (C-2 write) → 5 → 6 → 7 → 8
    Recommended: 1.5b (gate) → 3 (explain) → 4 (write) → 2 (reviewer) → 5 → 6 → 7 → 8

Why: Step 3+4 delivers the felt-experience win immediately. Step 2's engine-side reviewer is the
higher long-term-quality win but the user-visible payoff is smaller per dollar of build. Step 1.5b
(golden eval set) gates Step 2 either way per §10.A.

**F. Test-data location update.** Test invoices, receipts and a Client Manager xlsx live in the
developer's local `${LEDGR_TEST_DOC_DIR}` (a real test-firm folder with a multi-sub-client
structure, kept outside the repo per memory `no-real-client-data-in-repo`). Bank statements live
in `${LEDGR_TEST_BANK_DIR}`. Live QA happens in the developer's own Slack workspace. The §10 eval
set references these via env-var-relative paths only — no real client/sub-client names committed.
Memory `cast-unity-test-data` carries the concrete mapping privately.

**G. Step 3 shipped 2026-06-15 (C-1 explain + lookup).** Five read-only chat tools added to
`assistant_agent`: `explain_categorization`, `explain_tax_treatment`, `summarize_recent_activity`,
`lookup_row`, `list_recent_documents`. Build followed the §0.5-E reorder (1.5b → **3** → 4 → 2).
Live Slack QA prompts for Step 3: (1) "why did the Acme invoice go to 6100?", (2) "what tax code
should a non-registered client get on this SR line?", (3) "show me last month's activity",
(4) "find the AWS line", (5) "what documents have I processed this month?"

---

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

### 3.1 Extraction-engine reality (evidence-mapped 2026-06-15)

A direct code read of the extractors answers "does a NEW kind of document break it?" — the answer
is three-layered and matters for where we add intelligence:

- **The reader IS adaptive.** Extraction is a multimodal LLM (`gemini` flash, temperature=0) that
  *looks* at the PDF/image against a fixed Pydantic schema — NOT per-vendor templates or hardcoded
  Python parsing (`invoice_processing/extract/invoice_extractor.py:200`, `_BUNDLE_PROMPT:157`;
  `bank_statement_extractor.py` hybrid digital/vision). A never-seen invoice/receipt/bank LAYOUT of
  a KNOWN type reads fine. Multi-invoice PDFs, rotated multi-receipt scans, SOA packages, and
  multi-account statements are explicitly handled. **New layouts are a strength, not a break risk.**
- **The structure around it is rigid.** Output schema is fixed; doc types are a CLOSED list
  (`invoice, receipt, bank_statement, credit_note, statement_of_account, other` —
  `document_classifier.py:24`); a type not in the list is silently coerced to `"other"` and still
  routed down the invoice path. Tax is pure hardcoded Python + a YAML substring-keyword list,
  defaulting to `"SR"` (0.4 conf + flag) on anything unfamiliar (`tax_classifier.py`).
- **The real gap is SILENT failure, not crashes.** When the adaptive reader meets something that
  doesn't fit the rigid structure, it emits a wrong-but-valid result and passes it forward: unknown
  type → processed as invoice; 0 lines / 0 invoices → empty result flows downstream with no retry
  and no "I'm unsure"; novel bank column layout → possible silent data loss. A malformed LLM
  response (rare) raises a Pydantic error that is CAUGHT at the handler
  (`slack_runner.py:1210`/`1437`) — so the **bot never crashes**, but that document silently gets no
  result. In accounting, a silently-wrong number is worse than a loud crash.

**Implication (drives §6/§7):** the engine already has the intelligent part (an LLM reader). What's
missing is the *inspector that notices when the reader struggled* (empty lines, unreconciled totals,
low confidence, coerced `"other"`, 0 records) and retries-with-hints or asks the user — i.e.
**E-Move 1 (extract reviewer)** is the direct, evidence-backed fix, and it must fire only on those
cheap deterministic struggle-signals so the happy path stays free. A second target this surfaces:
**doc-type flexibility** — handle credit notes / purchase orders / payslips with the right shape
instead of jamming everything into the invoice schema.

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
| Multi-turn chat that remembers the thread | **Standalone root LlmAgent on its own Runner** (no `mode` → `include_contents='default'`), per-thread session `{channel}:chat:{thread_ts}`. A multi-turn chat agent CANNOT be a coordinator-graph node in ADK 2.x — "just drop `single_turn`" crashes graph build. See **ADR-0008** | sessions; llm-agents; ADR-0008 |
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

| # | Step | Surface | Outcome | Status |
|---|---|---|---|---|
| 1 | Rename `qa_agent` → `assistant_agent`; run it as a **standalone root agent on its own Runner** (`accounting_agents_assistant` App), per-thread session `{channel}:chat:{thread_ts}` with UTC-day fallback for channel-root messages, OUTSIDE the coordinator graph (**ADR-0008** — an in-graph multi-turn agent crashes graph build); seed client profile into chat; add 3 read tools (`show_client_profile`, `show_learned_mappings`, `model_info`) | C-0 | The chat helper knows who the client is and remembers the thread | ✅ **DONE 2026-06-15** (879 tests, live-QA'd) |
| 1.5a | Align HITL Edit DTO to canonical `InvoiceLine` keys (`tax_treatment`, `net_amount`) — pre-fix every Edit silently no-op'd against the exporter columns | E/C | Edit clicks actually change the books | ✅ **DONE 2026-06-15** (regression test added) |
| 1.5c | Tax classifier master-gate: non-GST-registered client → `NT` for ALL lines (purchase + sales), overriding doc content; Xero NT → "No GST" | E | Wrong-number bug for non-reg clients eliminated | ✅ **DONE 2026-06-15** (6 new tests) |
| 1.5b | Golden eval set from the developer's local test-firm docs (invoices + bank statements) as the gate for Step 2; `tool_trajectory_avg_score`, `final_response_match_v2`, `hallucinations_v1` per §10 | E/C | Measurable correctness baseline before adding any LLM-in-edge | ✅ **DONE 2026-06-15** (10-case evalset + pytest gate) |
| 2 | **Extract reviewer + retry-with-hints** between extract and categorize (Generate-and-Review / small LoopAgent). Verdict `{ok / hints_needed / user_clarify}`; mid-flow HITL on `user_clarify`. **Must skip `tax_*` signals when `our_gst_registered is False` (§0.5-C)** | E-1 | The engine checks its own work — the single highest-leverage move | not started |
| 3 | Explain + lookup read tools (`explain_categorization`, `explain_tax_treatment`, `summarize_recent_activity`, `lookup_row`, `list_recent_documents`) — reuse the engine's own categorizer/tax logic | C-1 | The assistant can explain *why*, grounded in the same engine | ✅ **DONE 2026-06-15** (17 unit tests, 903 fast suite green) |
| 4 | Write gate + `amend_ledger_row` / `remove_ledger_row` via ADK Tool Confirmation (smoke-test Firestore session first; fallback `adk_request_input`). Audit every write. New ADR-0009. | C-2 | The assistant gets hands — can fix the book, safely, with one-click confirm |
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
  delegation. `mode="single_turn"` forces `include_contents='none'`; a root agent with NO `mode` →
  `include_contents='default'` (sees history). VERIFIED (ADK 2.2.0): a multi-turn (`chat`) agent
  CANNOT be a downstream graph node — `workflow/_graph.py:520-538` raises `ValueError`; graph nodes
  must be task/single-turn, and `task` is disabled in graph workflows. So multi-turn chat = a
  STANDALONE root agent + per-thread session, NOT "drop single_turn in place". See ADR-0008 and
  memory [[adk-chat-mode-graph-constraint]].
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
- **Not** using inventive field names across boundaries. **Every DTO that crosses
  chat / Slack / graph / exporter MUST use the canonical `InvoiceLine` field names**
  (`tax_treatment`, `net_amount`, `account_code`, `description`). The pre-2026-06-15 mismatch
  silently no-op'd HITL Edit decisions — added as §0.5-D. Regression test:
  `tests/test_nodes.py::test_apply_decision_node_edit_does_not_silently_drop_canonical_fields`.
- **Not** trusting `MODEL_LITE` to always speak after a tool call. The Step 1 Gemini-flash-lite
  silence pattern (§0.5-B) requires both an explicit "always reply" instruction AND a runner-side
  safety net that surfaces the last tool result; carry this pattern into Steps 2, 5, 7.
- **Not** ignoring `our_gst_registered` in tax classification. §0.5-C: a non-GST-registered client
  gets `NT` on every line regardless of doc content. Step 2 reviewer must respect this; Step 4
  write tools must re-run the classifier on amended lines.

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
