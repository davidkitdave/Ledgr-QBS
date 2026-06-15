# Plan: Reshape Q&A as a general Accounting Agent (engine-as-tools)

**Status:** planning, no code yet. Companion: this branch's 12 shipped fixes (UX/dedup/Q&A
data) — F1 added the FIRST bank-aware Q&A tool; this plan generalises that move.

## 1. The problem in one paragraph

Today, what happens in chat is determined entirely by the **pipeline** (the engine). A file upload
runs the deterministic graph: `classify → extract → categorize → tax → approval_gate → route →
consolidate → deliver`. A text message routes to `qa_agent` (single-turn, read-only, 4 tools).
A casual reply in a thread saying "actually, change the Sep 5 row to Office" is treated as a Q&A
question, has no matching tool, and gets a polite punt. When extraction misses something on a new
vendor format, the only way to fix it is to manually tune `invoice_processing/extract/...`.

The user wants the engine to remain the workhorse for cold uploads, **but the agent to become the
brain in chat** — context-aware, multi-modal, able to invoke individual engine capabilities as
tools when the user asks. New features (chat-amend, re-extract with hints, learn from chat, explain
a categorization) then become tools the agent calls, not new nodes in the graph.

## 2. What the agent must know to feel "intelligent"

All of this already exists per-channel; we just need to surface it to the agent's system prompt
and tool context (`invoice_processing/export/client_context.py`):

- **Identity:** `client_name`, `client_uen`, `region` (SG/MY), `base_currency`, `slack_team_id`.
- **Business shape:** `accounting_software` ("QBS Ledger" / "Xero"), `tax_registered` (GST/SST),
  `fye_month` (drives FY routing).
- **Chart of Accounts:** the full `coa` (codes + descriptions + nature) — so the agent can reason
  "this looks like a software subscription, code 6100 fits".
- **Learned mappings:** `category_mapping` (vendor → account_code) and `entity_memory`
  (vendor → tax_code / role). These are the durable record of *what this client teaches us*.
- **Recent work:** the channel's ledger pointers (per-FY workbook), `seen_doc_keys`, and the in-
  thread context (which document this thread is about, if any).
- **Model identity (transparency):** which Gemini model is answering (lite vs pro vs thinking),
  exposed via a `model_info` tool — so when the user says "what model am I talking to?" the agent
  has a real answer instead of guessing.

ADR continuity: this is just richer use of the profile from ADR-0006 (per-client COA onboarding)
and the learned corrections from ADR-0004 (structured corrections, not memory-bank). Nothing new
to store; the agent reads what we already have.

## 3. Architecture choices — and the one I recommend

Three plausible shapes; I recommend (C). They're not mutually exclusive — (C) can absorb (B).

| | Shape | Pro | Con |
|---|---|---|---|
| **A** | One mega LlmAgent with every tool | Simplest mental model | Tool-pollution; safety-sensitive writes mixed with read-only Q&A; harder to constrain |
| **B** | Coordinator routes to specialised single-turn agents (read / amend / explain / re-extract) | Each lane is constrained and testable | The router becomes the bottleneck; harder for the agent to combine tools across lanes |
| **C** | **One `accounting_agent` (multi-turn LlmAgent) with a curated tool set, gated writes, and a "skills router" callback** | Conversational, can chain tools, retains the safety of (B) via the write gate | Needs careful instruction + per-turn budget guard |

Why (C): the user's examples ("ask why the extraction is X", "amend a row", "re-extract this
with a hint") all want the SAME agent to keep context across 2–3 turns and use whatever tool fits.
A single multi-turn agent with structured write confirmation is closer to how Claude/Gemini feel
in chat. The coordinator's `intent` schema (`document` / `question` / `unknown`) stays — but
`question` becomes "anything text-driven" and routes to `accounting_agent`, which then chooses
tools internally.

### Picture
```
                       ┌────────────────────────────────────────────────┐
upload  ──► coordinator ── document ──► engine pipeline (unchanged)
                       │
text  ──►              ── chat     ──► accounting_agent  (multi-turn)
                       │                    │
                       │                    ├── READ tools  (no confirm)
                       │                    ├── WRITE tools (confirm gate, 1 click)
                       │                    └── REASON tools (model_info, explain)
                       └── unknown ──► fallback ("upload a file or ask me about your books")
```

The engine pipeline stays the **system of record path**: deterministic, audited, HITL-gated
(ADR-0001/0003). The agent never re-implements that pipeline — it calls the same
`SlackLedgerStore.append_rows`, `categorize_node` logic, etc. under the hood.

## 4. Tool inventory (grouped by capability)

Naming convention: `verb_noun`, lowercased, returned as JSON. All tools pure where possible;
side-effecting tools (workbook writes) require explicit user confirmation via the write gate.

### Read tools — already exist (✅) or are small additions (➕)

| Tool | Purpose | Status |
|---|---|---|
| `bank_totals(month?, year?)` | withdrawals/deposits/net/opening/closing | ✅ F1 |
| `summarize_by_category()` | totals per COA / category | ✅ |
| `pnl_for_fy()` | revenue, expenses, net | ✅ |
| `gst_threshold_check()` | SGD 1 M GST registration headroom | ✅ |
| `lookup_row(month?, vendor?, description?)` | find a specific transaction | ➕ |
| `list_recent_documents(limit?)` | "what did I upload this week?" | ➕ |
| `show_client_profile()` | name, FYE, software, GST status, COA size | ➕ |
| `show_learned_mappings(vendor?)` | what mappings are remembered (entity_memory) | ➕ |
| `model_info()` | which Gemini model is replying (transparency ask) | ➕ |

### Reason / explain tools

| Tool | Purpose |
|---|---|
| `explain_categorization(month, vendor)` | "why did 5 Sep go to Office?" — re-runs `categorize_node` logic and exposes the matched rule (keyword / learned mapping / fallback) |
| `explain_tax_treatment(month, vendor)` | re-runs `tax_node` logic with the doc's data and returns reasoning |
| `summarize_recent_activity(days=7)` | a plain-English brief: "you posted N invoices, M bank statements; biggest categories were X/Y; one row needs review" |

### Write tools — gated; require a one-click confirmation card

Every write tool returns a `{"confirmation_id", "preview", "warning"?}` payload, NOT applying the
change. The agent posts a confirm card ("Apply this change? Yes / No"); only after the click does
the tool re-resolve from `confirmation_id` and apply. This mirrors the existing HITL Approve/Edit
pattern (ADR-0007).

| Tool | What it does |
|---|---|
| `amend_ledger_row(month, match, set)` | edit account_code / tax / description / amount of a posted row; re-runs balance math and re-uploads the workbook (uses the existing `_recompute_balances` + `rebuild_account_sheet`) |
| `remove_ledger_row(month, match)` | delete a row; same re-upload path |
| `re_extract_document(file_id, hints?)` | re-run extraction on a stored doc with optional hints ("this is a credit note", "the GST is 9% inclusive"); the result goes back through the engine but enters at the post-extraction stage |
| `replace_recorded_month(month)` | the "re-process this" path from F6 — clears the `seen_doc_keys` entry for that statement so a re-upload appends fresh |
| `learn_mapping(vendor, account_code, tax_code?)` | add a row to `category_mapping` / `entity_memory` so future docs auto-apply |

### Engine-call tools (the part that makes the agent flexible)

Each engine node already has its inputs/outputs well-defined in `nodes.py`. Wrap the **pure
parts** as tools the agent can call on already-loaded data:

| Engine node | Tool wrapper | When the agent would call it |
|---|---|---|
| `classify_node` | `classify_uploaded_doc(file_id)` | "what kind of doc was this?" |
| `extract_invoice_node` | `extract_invoice(file_id, hints?)` | "re-extract this with hints" |
| `extract_bank_node` | `extract_bank_statement(file_id, hints?)` | same |
| `categorize_node` | inside `explain_categorization` / `amend_ledger_row` | when user asks why or amends |
| `tax_node` | inside `explain_tax_treatment` | when user asks why |

The agent never re-implements these — the tools are thin wrappers that call the existing functions.

## 5. Safety — the write gate

A read-only Q&A can't break the workbook. A chat-driven amend can. The discipline:

1. **No write tool applies the change directly.** It returns a `{confirmation_id, preview, warning}`.
2. **The agent always posts a Slack confirmation card** with Yes / No buttons (re-using the
   existing HITL block builder from `app/blocks.py`).
3. **`confirmation_id` is a short-TTL Firestore doc** (≤10 min) holding the resolved tool args; the
   button click looks it up and applies. Prevents replay / cross-thread confusion.
4. **Idempotency:** every write tool is idempotent on `confirmation_id` (already a pattern in our
   resume path — `tests/test_resume_idempotency.py`).
5. **Audit:** every applied write is logged with `(channel_id, user_id, tool_name, args, result_file_id)`.

This is the same shape as the document HITL Approve/Edit/Reject flow (ADR-0007), just moved to
the chat lane. The user already understands that interaction.

## 6. Context, memory, model selection

- **Context per turn:** the runner seeds the session like the document lane does — `**profile_delta`
  (client_id/name, FYE, COA, learned mappings) PLUS the ledger snapshot `ledger_data`. The agent's
  system prompt embeds *who the client is* + *what tools are available*.
- **Multi-turn within a thread:** keep one session id per Slack thread (`thread_ts` as
  `session_id`), so the agent remembers "we were just discussing the Sep 5 row" across 2–3 replies.
  Today each question gets its own session — that's why it can't follow up.
- **Model tiering:** read/explain tools stay on `MODEL_LITE` (cheap, fast). Write tools and any
  re-extraction call use `MODEL_STD` or `MODEL_PRO` selectively via a tool-level model override.
  Aligns with the existing `model-tiering-and-no-a2a` memory.
- **Transparency:** `model_info()` returns the resolved model id — solves "what am I chatting
  with?" without guessing.

## 7. Phasing — ship small, ship often

Each phase commits standalone behind unit tests and a live verification step. Stops are OK between
phases — the system stays usable.

**Phase 0 — name + multi-turn (no new tools)**
- Rename `qa_agent` → `accounting_agent` (`MODEL_LITE`).
- Switch chat session id from per-message → per-thread.
- Seed `**profile_delta` into the chat session (so the agent knows client name / COA / FYE).
- Add `show_client_profile`, `show_learned_mappings`, `model_info`. Live-verify with chat.

**Phase 1 — explain tools**
- `explain_categorization`, `explain_tax_treatment`, `summarize_recent_activity`,
  `list_recent_documents`, `lookup_row`. All pure / read. Live-verify on Akar with real history.

**Phase 2 — write gate + amend**
- Generic confirmation-card helper in `app/blocks.py`.
- `amend_ledger_row` + `remove_ledger_row` behind the gate, with audit log.
- New ADR (0008): "chat-amend posted ledger" — references this plan.

**Phase 3 — re-extract & learn**
- `re_extract_document(file_id, hints)` and `replace_recorded_month(month)`.
- `learn_mapping(vendor, …)` — writes to `category_mapping` / `entity_memory` (existing learning
  path from ADR-0004), gated.

**Phase 4 — better extraction without manual tuning**
- The agent inspects an extracted result, sees a low-confidence field, and proposes a
  hint-driven re-extract automatically (with confirmation). This is the "smart, knows what to do"
  behaviour the user asked for, but built on top of Phases 0–3.

## 8. Open decisions (need answers before Phase 0)

1. **Scope of multi-turn memory:** thread-scoped (`thread_ts` = session) vs channel-scoped?
   Recommendation: **thread**, so different threads don't pollute each other.
2. **Confirmation TTL:** 10 minutes feels right (HITL parity). Push back if you'd rather make it
   single-click immediate for low-risk edits (e.g. description-only).
3. **Who can confirm a write?** Only the user who triggered it, or anyone in the channel? Recommend
   **triggering user only** for safety.
4. **Model for the chat agent:** start LITE; promote to STD when a tool actually re-extracts a
   document. Sticking on LITE throughout will be cheaper but worse at chained reasoning.
5. **Renaming vs new name:** rename `qa_agent` → `accounting_agent` is honest (it's the same
   socket, broader role). Code is internal; no external contract breaks.
6. **Where does this ADR live?** ADR-0008 once Phase 2 lands (write gate is the irreversible move).
   This plan is a *pre-ADR design memo* and stays in `docs/qa/` until then.

## 9. Anti-goals (what this plan is NOT)

- Not a rewrite of the deterministic engine. Cold uploads still flow through the pipeline; ADR-0001
  stays.
- Not a free-form "agent does whatever it wants" loop. Tools are explicit; writes are gated.
- Not a place to keep adding nodes. New behaviour lives as **tools the agent can call**, not new
  graph nodes — that's what "engine as tools" means.
- Not a replacement for HITL on cold uploads. The pre-post Approve/Edit card stays exactly as it is.

---

## Asks for the user (so I can start Phase 0 cleanly)

1. Confirm the recommended shape (C): one `accounting_agent`, gated writes, engine-as-tools.
2. Confirm the **5 decisions in §8** (or override).
3. Confirm the phasing order. If you want chat-amend (Phase 2) sooner, I can move Phase 1 inline
   with it — they touch different files.

Once those are answered, I'll start Phase 0 (rename + multi-turn + 3 small read tools) on a fresh
branch and report back before touching writes.
