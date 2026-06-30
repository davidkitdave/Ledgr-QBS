# 0026 — AI reads, deterministic rules apply (YAML data) on a lean LlmAgent; retire the Workflow graph

- **Status:** Accepted (Phase A light path — 2026-06-27)
- **Date:** 2026-06-24
- **Deciders:** Ledgr team
- **Supersedes (in part):** ADR-0001 (graph-as-root packaging), ADR-0003 (RequestInput HITL),
  ADR-0021 (`root_agent = document_workflow`), ADR-0025 §WS-0.4 (HITL primitive). Their
  *principles* are retained; their *graph packaging* is not. See "Reconciliation" below.

## Context

Two questions kept recurring: (1) *which* agent shape is "correct ADK" — the heavy
`accounting_agents` `Workflow` graph or the lean `ledgr_agent` (`LlmAgent` + one
`process_document_batch` tool) — and (2) how to make the system feel *intelligent*
rather than "full of hardcoded regex/keyword Python," using AI + YAML + skills.

Investigation (codebase + ADK docs MCP + Google developer-knowledge MCP) found:

- **The engine is already LLM-intelligent.** Classification and extraction already use
  Gemini structured output (`response_schema`) in `invoice_processing/classify` and
  `/extract`. The "dumb regex" feeling comes from the **monolith wrapper**
  (`slack_runner.py` ~285KB, the graph in `agent.py`, `nodes.py` ~113KB), not the brain.
- **Both agents call the same deterministic engine** (`invoice_processing.*`).
  `ledgr_agent`'s tool wraps it; the graph's nodes wrap it. Switching shape does **not**
  change extraction intelligence — only maintainability/testability.
- **The graph's only unique value was native ADK `RequestInput` pause/resume HITL**
  (ADR-0003) — and the Slack layer already runs its **own** Firestore interrupt bridge
  (`hitl.py`: `write_interrupt`/`resume_session`) that coordinates the same pause/resume.
  The graph `RequestInput` node is largely redundant ceremony on top of that bridge.
- **A separate greenfield (`Ledgr-Agentic`) is *not* smarter** — its engine is the same
  kind of deterministic Python + structured output, younger (4.3K LOC, 15 test files vs
  QBS's 8.7K engine + 122 test files), with **zero Slack** and a from-scratch tax/SST
  reimplementation (correctness-regression risk on the exact tax/COA cases we care about).

**Grounded ADK guidance (sources below):** *"weave deterministic code with adaptive AI
reasoning"*; *"for tasks requiring predictable outcomes — such as financial calculations
or tax rules — logic should be implemented in deterministic code"*; structured output
*"replaces manual regex parsing"*; Skills package instructions + `assets/` (templates,
schemas) loaded on demand. Google's own tool example instructs the model *"never attempt
to determine [the answer] manually… you must call the tool."*

## Decision

**1. The single production agent is the lean `ledgr_agent`** — an `LlmAgent` coordinator
whose deterministic work lives in `process_document_batch` (and sibling tools), which
calls the proven `invoice_processing.*` engine. The `accounting_agents` `Workflow` graph
is retired (eval-gated cutover, Plan 6). `Ledgr-Agentic` is treated as a **reference
prototype** (mine its `erp_export_skill` YAML design and payroll ideas), not promoted.

**2. Four-layer "right and light" architecture** — the right tool for each job:

| Layer | Job | Tool |
|---|---|---|
| **Brain** | route, talk, decide which tool | `LlmAgent` (Gemini) |
| **Read** | messy document → structured data | Gemini `response_schema` + `enum` (**replaces regex/keywords used for reading**) |
| **Apply** | tax code, COA, reconcile, FX | **thin deterministic Python** (auditable, reproducible) |
| **Rule-data** | tax rates, ERP column maps, COA | **YAML** (jurisdiction policies + ERP profiles, packaged as Skill `assets/`) |
| **Guard** | validate before commit | `after_tool` callback ("fail loud" vs the YAML policy) |

**3. The line that does not move:** the LLM **reads**, it never **decides tax/COA codes**.
Tax/SST/GST classification and reconciliation stay deterministic Python driven by YAML
data, because clients and auditors require the same answer every time and an explanation.
"No Python rules at all" is explicitly rejected as un-auditable.

**4. HITL** is realized by the **existing Firestore interrupt bridge** (`hitl.py`),
triggered by the batch tool's `pending_reviews` + the Slack review card — **not** by an
ADK graph `RequestInput` node (retired with the graph) and **not** by ADK Tool
Confirmation (still unsupported with persistent Firestore sessions). The Slack review/edit
surface (ADR-0007) and edits-become-Corrections (ADR-0004) are unchanged.

**5. Eval is the gate.** A single golden eval set (real SG/MY docs; deterministic field
match for doc-type, fields, tax/COA — *not* LLM-as-judge) is the regression gate for both
the engine fixes and the cutover (the "eval + live QA pass" Plan 6 already requires).

### Phase A — light path only (2026-06-27)

`ledgr_agent` is trimmed to three ADK tools:

1. **`read_doc`** — one Gemini read (`ReadDocumentBundle` schema); sets `file_kind`
2. **`build_sheets`** — unified workbook rows (ERP YAML skills or bank tabs) in `session.workbook`
3. **`read_credit_balance`**

Removed from the package (not duplicated in-tree): `process_document_batch` factory,
`policies/ledger/` full COA/tax/route engine, batch metrics, and duplicate read/project
tools. A **`process_document_batch` legacy stub** remains for unchanged Slack imports until
the Slack wiring plan lands. Full tax/COA/SOA batch may return as a single module or via
`legacy/` — not as 15 duplicate folders.

## Consequences

- Production runs one small, testable agent over a proven engine; the ~11K LOC of
  graph/Slack-glue shrinks to the Slack bridge + the tool.
- Extraction quality is owned by the engine + eval, independent of agent shape.
- Rule changes (a new SST rate, an ERP column) become **YAML edits**, not code changes.
- We keep all hard-won correctness (SST 2025 regime, ERP exporters, Firestore isolation,
  lease locks) instead of re-paying for it in a greenfield.
- We carry the `LEDGR_USE_CLEAN_AGENT` flag until the eval-gated soak completes, then retire
  both the flag and the graph.

## Reconciliation with prior ADRs

- **ADR-0001** — *retained:* the engine is deterministic and is never re-expanded into
  per-step LLM nodes. *Superseded:* "the runtime root is a slim Workflow graph" — the root
  is now the `LlmAgent`; the engine is a tool, not a graph node.
- **ADR-0003 / ADR-0025 §WS-0.4** — *superseded:* HITL is no longer an ADK `RequestInput`
  graph node. The `hitl.py` Firestore bridge it built is *retained* and now driven by the
  tool's `pending_reviews`. (ADR-0003's own rejected alternative — "bare `LlmAgent`, Engine
  as a tool" — is the option we now adopt, because the Firestore bridge, not a graph node,
  provides the pause/resume.)
- **ADR-0007** — *retained:* the Slack review/edit card and the resume semantics; only the
  upstream pause primitive changes (tool `pending_reviews`, not a graph node).
- **ADR-0021** — *retained:* deterministic document entry, no LLM `RouteDecision`. In Slack,
  the file event still deterministically triggers processing. *Superseded:*
  `root_agent = document_workflow`; the discoverable root is now `ledgr_agent`.
- **ADR-0011** (Understand layer) and **ADR-0005/0019** (canonical schema → per-target
  projection) are *reinforced*, not changed — they are the "Read" and "Rule-data" layers.

## Alternatives considered

- **Keep the `Workflow` graph as the engine** — its only unique value (native HITL) is
  already covered by the Slack Firestore bridge; rejected as redundant ceremony.
- **Adopt `Ledgr-Agentic` as home** — months of work to port Slack + re-validate a younger
  from-scratch tax engine, with real correctness-regression risk; rejected. Mine its skill
  design instead.
- **Let the LLM decide tax/COA codes ("no Python rules")** — un-auditable, non-reproducible;
  rejected on accounting-correctness grounds, consistent with ADK guidance.

## Addendum (2026-06-29) — eval gate

The production quality gate for `ledgr_agent` is now [ADR-0033](0033-reference-free-ledgr-agent-eval.md):
reference-free grading (`extraction_self_consistency` + `extraction_faithfulness` + ADK rubric/
hallucinations metrics). The `LEDGR_USE_CLEAN_AGENT` cutover flag language is retired — the lean
agent path is the default surface under test.

## Sources

- ADK — Function tools / division of labor: `adk.dev/tools-custom/function-tools`
- ADK — Workflow vs LLM agents, SequentialAgent: `adk.dev/agents/workflow-agents/sequential-agents`
- ADK — Skills (`SKILL.md`, `assets/`, on-demand load): `adk.dev/skills`
- ADK — Agent Config (YAML, experimental, Gemini-only): `adk.dev/agents/config`
- ADK — Callbacks (guardrails): `adk.dev/callbacks`
- Gemini — structured output / controlled generation: `ai.google.dev/gemini-api/docs/structured-output`
- Google Cloud — choosing agentic architecture: `cloud.google.com/architecture/choose-agentic-ai-architecture-components`
