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
pytest tests/eval/test_g_extraction_golden.py -m eval   # G-cluster, live PDF extraction (WS-0.2)
```

Document behaviour is also covered by `process_file_event` integration tests outside this
directory: `tests/test_ws2_ws3.py`, `tests/test_pipeline.py`, `tests/test_concurrency.py`.

**Adding a document case:** add an `A/C–F` case to the evalset (graded by `AgentEvaluator`
via `eval_config.yaml`, or by the offline F-cluster scorers in `custom_metrics.py` via
`eval_config_adr0015.yaml`), and/or a `process_file_event` integration test. Do **not** add it
as an agents-cli eval case — `_select_case_ids(lane="doc")` raises on purpose.

**Adding an extraction golden case (G-cluster, WS-0.2):** add a `G<n>_...` case to
`ledgr.evalset.json` with `_eval_assertions` in `session_input.state`, register expected
values in `tests/eval/extraction_metrics.py` (`_G_CASE_TABLE`), and map the local PDF via
`SCENARIO_PDF_RELATIVE` (PDF stays on `~/Desktop/LocalTest/` — never commit). Grade with
`eval_config_extraction.yaml` custom metrics (`doc_count_score`, `extraction_totals_score`,
`page_coverage_score`). Run `pytest tests/eval/test_g_extraction_golden.py -m eval`.

## Configs (two runners, by design)

- `eval_config.yaml` — the **pytest `AgentEvaluator`** path (tool-trajectory + a
  `custom_response_quality` LLM judge).
- `eval_config_adr0015.yaml` — the **offline F-cluster** rubric + deterministic scorers.
- `eval_config_extraction.yaml` — the **G-cluster** extraction golden gate (WS-0.2:
  `doc_count_score`, `extraction_totals_score`, `page_coverage_score`).
- `eval_config_chat.yaml` — the **agents-cli** chat grade (subsequence tool-trajectory match +
  response-quality judge).

These are intentionally separate because they feed different runners. Known dead entries in
`eval_config.yaml` (`response_match_score` criterion, `agent_turn_count` metric) are unused;
they are left in place rather than removed to avoid perturbing the gated pytest path.

## Line-level deterministic eval (issue #28)

The document lane also has a **field-match scorer** for per-line tax treatment, COA, and
direction — separate from the graph `AgentEvaluator` path above.

**Join key:** every export row carries a stable `source_doc_id`
(`invoice_processing/export/source_doc_id.py`):

```
{basename}:{reference}:{start}-{end}
```

`reference` is the invoice number; when missing, falls back to `i{index}`. The Slack
`file_id` is deliberately **not** used (it rotates per upload).

**Where rows are tagged:** `invoice_processing/export/exporters.py` stamps
`ROW_PROVENANCE_KEYS` (`source_doc_id`, `tax_treatment`, `account_code`, `direction`)
on each exported row.

**Scorer:** `ledgr_agent/metrics/golden_field_match.py` groups rows by `source_doc_id`
in `project_batch` and scores line fields deterministically (ADR-0026 §5).

**Fixture:** `tests/eval/datasets/golden_line_level_sample.json`

**Run:**

```bash
uv run pytest tests/ledgr_agent/test_line_level_doc_id_eval.py -q
```

Legacy golden manifests without `source_doc_id` still score at document level only; new
cases should author `source_doc_id` on each expected document.

## Current state (2026-06-29)

- Chat lane: **6/6 cases (B3–B8)** score tool-trajectory `1.0`, response-quality `5.0`.
- Document lane: golden + F-cluster gates green; integration tests cover fan-out/fan-in,
  dedupe, and per-FY ledger writes.
- Line-level eval: `test_line_level_doc_id_eval.py` gates `source_doc_id` tagging and
  per-line field-match scoring (issue #28).
- Coverage to grow: more chat cases for MY jurisdiction, SOA, and credit-note Q&A (the doc
  lane already has `F11_credit_note_sales` and `F12_MY_receipt`).
