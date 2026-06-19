# Ledgr evaluation — how it actually works

Ledgr has **two runtime surfaces**, so it has **two eval lanes**. See ADR-0023 for the
rationale; this file is the operational guide.

| Lane | Surface | Eval tool | Why |
|---|---|---|---|
| **Chat (cluster B)** | `accounting_agents/chat_eval` — an `LlmAgent` answering text prompts | **agents-cli** (`generate` → `grade`) | Conversational; cases carry a prompt. `compare`/`analyze`/`optimize` apply here. |
| **Document (clusters A, C–F)** | `accounting_agents.agent` — a graph `Workflow` driven by `process_file_event(pdf)` | **pytest** (`AgentEvaluator` + offline scorers + integration tests) | The pipeline ingests a PDF, not a chat turn — `agents-cli eval generate` does not model it. |

All cases live in one file, `tests/eval/datasets/ledgr.evalset.json` (current ADK `EvalSet`
schema). The lane is decided by the case-id prefix: **`B*` = chat, everything else = document**
(`tests/eval/eval_routing.py::is_chat_case`).

## Run the chat lane (agents-cli)

```bash
./scripts/ledgr_eval_chat.sh                 # all B* cases (auto-derived from the evalset)
./scripts/ledgr_eval_chat.sh B6_chat_...     # a single case
```

This runs ADK inference with each case's seeded `session_input.state`
(`eval/ledgr_eval_generate.py`), writes `artifacts/traces/chat_traces.json`, then grades with
`agents-cli eval grade --config tests/eval/eval_config_chat.yaml`. Results land in
`artifacts/grade_results/results_<ts>.{json,html}`.

To compare before/after a change:

```bash
agents-cli eval compare <prev_results>.json <new_results>.json
```

**Adding a chat case:** add a `B<n>_...` case to the evalset. It is picked up automatically —
chat-case selection derives every `B`-prefixed id from the evalset (do **not** reintroduce a
hardcoded list; that bug dropped B7/B8, see ADR-0023).

## Run the document lane (pytest)

```bash
pytest tests/eval/ -m eval -v                      # golden + GST + HITL (AgentEvaluator)
pytest tests/eval/test_f_extract_direction.py -m eval   # F-cluster, offline, no LLM
```

Document behaviour is also covered by `process_file_event` integration tests outside this
directory: `tests/test_ws2_ws3.py`, `tests/test_pipeline.py`, `tests/test_concurrency.py`.

**Adding a document case:** add an `A/C–F` case to the evalset (graded by `AgentEvaluator`
via `eval_config.yaml`, or by the offline F-cluster scorers in `custom_metrics.py` via
`eval_config_adr0015.yaml`), and/or a `process_file_event` integration test. Do **not** add it
as an agents-cli eval case — `_select_case_ids(lane="doc")` raises on purpose.

## Configs (two runners, by design)

- `eval_config.yaml` — the **pytest `AgentEvaluator`** path (tool-trajectory + a
  `custom_response_quality` LLM judge).
- `eval_config_adr0015.yaml` — the **offline F-cluster** rubric + deterministic scorers.
- `eval_config_chat.yaml` — the **agents-cli** chat grade (subsequence tool-trajectory match +
  response-quality judge).

These are intentionally separate because they feed different runners. Known dead entries in
`eval_config.yaml` (`response_match_score` criterion, `agent_turn_count` metric) are unused;
they are left in place rather than removed to avoid perturbing the gated pytest path.

## Current state (2026-06-19)

- Chat lane: **6/6 cases (B3–B8)** score tool-trajectory `1.0`, response-quality `5.0`.
- Document lane: golden + F-cluster gates green; integration tests cover fan-out/fan-in,
  dedupe, and per-FY ledger writes.
- Coverage to grow: more chat cases for MY jurisdiction, SOA, and credit-note Q&A (the doc
  lane already has `F11_credit_note_sales` and `F12_MY_receipt`).
