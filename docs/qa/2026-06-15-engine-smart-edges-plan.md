# Plan: Smart edges for the engine (honest assessment + ADK-grounded direction)

**Companion to:** `2026-06-15-accounting-agent-plan.md` (the chat-side agent). That plan reshapes
**how the user chats** with the bot. This plan reshapes **how the engine processes documents** —
specifically, whether the edges between pipeline nodes are intelligent or hard-coded.

Grounded in `adk-docs` MCP (citations in §6). Status: planning, no code yet.

---

## 1. The honest read of where Ledgr-QBS is today

I've worked on this codebase across many sessions. Here are the patterns I see, plainly.

### What's working

The deterministic engine is **good** for the 80–90% happy path. A clean OCBC bank statement or
a clean Singapore vendor invoice flows through `classify → extract → categorize → tax → route →
consolidate → deliver` quickly, cheaply, predictably. ADR-0001 (deterministic engine in a slim
workflow graph) was a sound call for a system of record. HITL Approve/Edit (ADR-0007) gives a
safety net at the end. The learning loop (ADR-0004 — `category_mapping` / `entity_memory`) does
work: the system gets quietly smarter for repeated vendors.

### What's structurally wrong, in plain language

1. **The pipeline marches forward without ever checking its own work.** Extraction may return
   garbage with 0.3 confidence; categorization then dutifully categorizes the garbage; tax then
   dutifully taxes it; the user only sees the mess at the approval card. There is no "review and
   maybe retry" step between any two nodes.

2. **HITL only fires at the END of the pipeline.** The Approve / Edit / Reject card is the user's
   only chance to intervene. They can't say "wait — re-extract that, it's a credit note" mid-flow.
   The only escape valve is to Reject, fix, and re-upload — losing the in-flight state.

3. **Edge-case handling is by accretion.** When something breaks, we add a band-aid: `N()`
   coercion on the Math_Check formula; `_is_formula_or_missing` guard for non-numeric balances;
   content-based `doc_key` to survive re-uploads; block-level dedup for the doc_key transition.
   Each fix is correct in isolation, but the engine's edges remain hard-coded — the next vendor
   layout we haven't seen will need another band-aid. This is exactly the "manual tuning every
   time" pain you described.

4. **The qa_agent is a separate, smaller brain.** It has 4 read-only tools and no access to the
   engine's capabilities. A user who chats can't trigger a re-extract, can't amend a posted row,
   can't ask the system to learn a vendor mapping from chat. The deterministic pipeline and the
   chat agent don't share a mind.

5. **Categorization is a keyword match with a fallback.** `categorize_node` matches against
   keywords + learned mappings + a default. It cannot reason — "this looks like a SaaS
   subscription, the vendor name ends in `.io`, and the line item says 'subscription'; that
   should go to 6100-Software even though we've never seen this vendor before." A small LLM
   nudge here would beat tens of new keyword rules.

6. **Tax determination is the same shape.** `tax_node` follows rules (SR/ZR/ES/OS/EP). Edge cases
   need new rules — instead of an LLM that reads the invoice, considers GST registration, and
   picks the treatment with a one-line justification.

7. **The `dynamic_router` is the dumbest edge possible.** It looks at `intent` ∈ {document,
   question, unknown} and routes. It cannot route mid-pipeline; cannot decide "this looks weird,
   send to HITL early"; cannot decide "this is a multi-invoice PDF, fan out before extracting".

### What ADR-0001 said vs what we now know

ADR-0001 said: keep the engine deterministic, put the LLM only at the leaves (extraction). That
was right when the goal was system-of-record reliability. **It is still right for the happy
path.** It's wrong for the edge cases — and we have evidence: every band-aid fix this branch has
shipped is an edge case the deterministic path mishandled.

The fix is **not** to throw the engine away. The fix is to make the **edges between nodes** capable
of small, local intelligence: review the previous step's output, decide whether to continue,
retry with hints, or ask the user.

## 2. What ADK actually offers (cited)

I queried the docs before forming an opinion. Five patterns are directly applicable.

### A. Dynamic Workflows (`adk.dev/graphs/dynamic/`) — the foundational primitive

ADK Python v2.0.0 introduced **Dynamic Workflows**: `@node` + `ctx.run_node()` + plain Python
control flow (loops, conditionals, recursion) with **automatic checkpointing for resume**. This
is exactly the primitive "smart edges" need.

```python
@node
async def smart_doc_workflow(ctx, file_id):
    cls = await ctx.run_node(classify_node, file_id)
    extr = await ctx.run_node(extract_node, cls)
    review = await ctx.run_node(review_extraction, extr)
    while review.confidence < 0.7 and review.retries < 2:
        extr = await ctx.run_node(extract_node, cls, hints=review.suggestions)
        review = await ctx.run_node(review_extraction, extr)
    if review.confidence < 0.5:
        hints = yield RequestInput("Uncertain extraction — can you clarify X, Y, Z?")
        extr = await ctx.run_node(extract_node, cls, hints=hints)
    cat = await ctx.run_node(categorize_node, extr)
    # ...
```

You write the orchestration logic as code; ADK handles the resume and journaling. The current
graph wiring in `accounting_agents/agent.py` could be migrated to this shape.

### B. Generate-and-Review (`adk.dev/workflows/patterns/#generate-and-review-pattern`)

Pair each "generator" (e.g. `extract_node`) with a "reviewer" (a small LlmAgent that checks the
output and writes a status to state). The next step branches on the status. This is the textbook
**smart edge** between two nodes.

```python
extractor = LlmAgent(name="extract", ...).with_output_key("extraction")
reviewer  = LlmAgent(name="review",
    instruction="Read {extraction}. Output 'ok' if all required fields are present and the totals "
                "reconcile, else 'needs_hints' with one line on what's missing.",
    output_key="extract_review")
```

### C. Iterative Refinement (`adk.dev/workflows/patterns/#iterative-refinement`)

`LoopAgent(max_iterations=N, sub_agents=[generator, checker, stop_checker])` — `stop_checker`
sets `escalate=True` when satisfied. Perfect for "extract until confidence ≥ threshold" or
"categorize until you find a match".

### D. Coordinator + LLM-driven delegation (`adk.dev/workflows/patterns/#coordinator-and-dispatcher`)

LlmAgent with `sub_agents=[...]` and clear `description` on each sub-agent. The LLM picks which
sub-agent should handle the turn. Our current `coordinator` uses this primitive but only at the
top level for intent. We could nest the same pattern *inside* the document pipeline — e.g. a
"resolver" sub-agent that decides "is this invoice a credit note, a regular invoice, or a
deposit?" and delegates to a specialized extractor.

### E. Tool Confirmation (`adk.dev/tools-custom/confirmation/`)

Already covered in the chat-agent plan: `FunctionTool(require_confirmation=True)` or
`tool_context.request_confirmation(hint, payload)` for mid-flow human approval. **Crucially**,
this works mid-pipeline too — a smart-edge reviewer can call a "ask the user" tool, pause, and
resume on the response. This replaces "HITL only at the end" with "HITL whenever the system is
uncertain".

### What I checked and won't use

- **`MemoryService`** — for unstructured cross-session recall. Our learning is structured (ADR-0004
  in Firestore). Keep what we have.
- **Python `RoutedAgent`** — TypeScript only today.
- **Custom Agents (`BaseAgent` subclass with `_run_async_impl`)** — older, lower-level way to do
  what Dynamic Workflows now does cleanly. Skip unless we need the escape hatch.

## 3. My recommended architecture (honest opinion)

**Not** "rip out the engine and replace with an agent". That would be slower, more expensive, less
predictable, and would throw away the work that makes Ledgr fast on the happy path.

**Instead** — three moves, in order, on the *existing* engine:

### Move 1 (smallest): Add a Reviewer between extract and categorize
- After `extract_invoice_node` (or `extract_bank_node`), insert a small `LlmAgent` reviewer.
- It reads the extraction + the source PDF reference and writes a JSON status:
  `{"verdict": "ok|hints_needed|user_clarify", "confidence": 0.0–1.0, "missing": [...], "warnings": [...]}`.
- If `ok` → continue to categorize (current path).
- If `hints_needed` → re-run extract with the reviewer's `missing` as hints, loop ≤2.
- If `user_clarify` → emit a `RequestInput` (mid-pipeline HITL) asking the *specific* question.

**ROI:** This single addition kills several classes of band-aid. The "non-numeric balance",
"Math_Check #VALUE!", "missing closing balance" failures we shipped fixes for would have been
caught by a reviewer before they ever reached the workbook.

### Move 2 (medium): Replace the keyword-match categorizer with a hybrid

- Keep the **fast path**: learned mapping → keyword match → no-match.
- On no-match, instead of falling back to a default, ask a small LlmAgent with three inputs:
  the line item text, the client's COA (already in profile state), and the entity_memory.
  It outputs `{account_code, why}`.
- Result goes to learning loop (entity_memory) automatically when the user approves.

**ROI:** Eliminates the "we keep adding keywords" treadmill. The LLM doesn't need to be huge —
`MODEL_LITE` is fine because the prompt is tight (COA + line + memory).

### Move 3 (larger): Migrate the pipeline orchestration to Dynamic Workflows

- Today: graph wiring (a chain in `agent.py` connecting nodes by edges).
- Tomorrow: one `@node async def smart_doc_workflow(ctx, file_id)` that calls each step
  explicitly with `ctx.run_node(...)`, branches on reviewer verdicts, can loop, can pause for HITL.
- Resume + idempotency: ADK handles checkpointing per `ctx.run_node` call. Side-effecting nodes
  (consolidate, deliver) stay idempotent via the existing dedup.

**ROI:** This is the "system is much more intelligent" target. Each edge can be replaced
independently — start with extract-review (Move 1), categorize-with-COA (Move 2), then add
reviewers between other steps as the pain demands.

### What stays untouched

- `SlackLedgerStore.append_rows` — system of record.
- HITL Approve / Edit / Reject card at the end — still there.
- Firestore profile / COA / entity_memory — same data model.
- `qa_agent` / `accounting_agent` chat-side reshape from v2 plan — proceeds in parallel.

## 4. Where the chat agent and the smart-edge engine meet

This is the unification the user asked for: "the agent needs to have the tools to go and do
whatever thing that the invoice processing had". With smart edges:

- The chat agent's `re_extract_document(file_id, hints)` tool calls the **same** `extract_node`
  the engine uses, with the same hints structure the in-pipeline reviewer uses.
- The chat agent's `explain_categorization` tool re-runs the **same** categorizer that fires in
  the pipeline, with the same COA + memory + line input.
- The pipeline's "user_clarify" RequestInput delivers a Slack card; the user's reply feeds the
  same agent that handles in-thread questions.

Both lanes share the same Firestore-backed knowledge (profile / COA / mappings / memory) and the
same node implementations — only the orchestration differs.

## 5. Phasing (revised — engine work fits between chat-agent phases)

Reordered so engine work and chat work interleave cleanly. The chat-agent v2 plan's Phases are
referred to as **C-Phase 0–4**; this plan's engine moves as **E-Move 1–3**.

| Step | Work | Why this order |
|---|---|---|
| 1. | **C-Phase 0** — rename `qa_agent` → `accounting_agent`, multi-turn, profile-seeded, 3 small read tools | Smallest, ships quickest; unblocks everything else |
| 2. | **E-Move 1** — extract reviewer with retry-with-hints (LoopAgent or Dynamic Workflow) | Highest-leverage single move on the engine; addresses your "no manual tuning" goal |
| 3. | **C-Phase 1** — explain/lookup tools (read) | Once engine reviewers exist, `explain_categorization` becomes obvious to implement |
| 4. | **C-Phase 2** — write gate + amend (uses ADK confirmation) | The chat-amend you asked about earlier |
| 5. | **E-Move 2** — hybrid categorizer (LLM on no-match) | Tackles the second-biggest source of "manual tuning" |
| 6. | **E-Move 3** — migrate orchestration to Dynamic Workflows | Now that we know the shape, encode it cleanly; enables further smart edges per node |
| 7. | **C-Phase 3** — re-extract & learn from chat | Now both lanes share the same engine tools |
| 8. | **C-Phase 4** — proactive auto-hints from low-confidence reviews | The "smart, knows what to do" outcome — built on everything above |

Each step is committable on its own and live-verifiable.

## 6. ADK doc citations (queried 2026-06-15 via `adk-docs` MCP)

- **Workflow patterns** (`adk.dev/workflows/patterns/`): Coordinator + dispatcher, Sequential
  pipeline, Parallel fan-out/gather, Hierarchical decomposition, **Generate-and-review**,
  **Iterative refinement** (LoopAgent + `escalate=True`), Human-in-the-loop.
- **Dynamic workflows** (`adk.dev/graphs/dynamic/`, Python v2.0.0+): `@node` + `ctx.run_node()`,
  while loops + conditionals, automatic checkpointing for resume, `parent` nodes must set
  `rerun_on_resume=True`, custom execution IDs available (use sparingly).
- **Graphs** (`adk.dev/graphs/`, v2.0.0+): graph-based workflows with edges; can compose Nodes
  (agents, tools, code, sub-workflows). Known limit: incompatible with Live Streaming.
- **Custom Agents** (`adk.dev/agents/custom-agents/`): older/lower-level orchestration via
  `BaseAgent._run_async_impl`. Functional but Dynamic Workflows supersede it for new code.
- **Tool Confirmation** (`adk.dev/tools-custom/confirmation/`): `FunctionTool(require_confirmation=...)`
  or `tool_context.request_confirmation(hint, payload)` — applies mid-pipeline too.

## 7. Anti-goals (this plan is explicit about what it is NOT)

- Not throwing away the deterministic engine. The happy path stays fast.
- Not putting an LLM in EVERY edge. Only where a deterministic rule is failing or accumulating
  band-aids — measure before adding.
- Not building a "fully autonomous" agent that runs the engine without checks. HITL stays as the
  escape valve, just now available earlier in the flow.
- Not rewriting the bank/invoice extractors. They become callable with hints.

---

## 8. My honest opinion in one paragraph

The current engine is a competent factory line: each station does one thing and passes the part
along. It breaks down when a part needs *thinking* between stations. The right move isn't to
replace the factory with one giant thinking robot — it's to put a small inspector between each
station who can call for a redo, ask for help, or wave the part through. ADK's Dynamic
Workflows + Generate-and-Review + tool Confirmation give us exactly those inspectors. Start with
the most painful seam (extract → categorize), measure the win, then add the next inspector.

## 9. Decisions I need from you to start

1. **Buy the architecture?** Smart edges between existing nodes, not a wholesale rewrite.
2. **Start with E-Move 1 (extract reviewer)?** That's the highest-leverage single move and is a
   1–2 day build behind tests. It would land BEFORE C-Phase 2 (chat-amend) in the phasing above.
3. **OK to keep the existing graph wiring and add the reviewer as a sub-step**, vs. migrating to
   Dynamic Workflows now? My recommendation: keep the wiring, slot the reviewer in as a SequentialAgent
   ([generator, reviewer, conditional-retry]) so the migration to Dynamic Workflows can wait
   until E-Move 3.
