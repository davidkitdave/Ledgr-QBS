# ADR-0033: Reference-free single-lane eval for `ledgr_agent`

**Status:** Accepted  
**Date:** 2026-06-29  
**Supersedes:** ADR-0015 (F-cluster gate), ADR-0023 (two-lane eval)

## Context

Per ADR-0032, `ledgr_agent` is the pure ADK agent (`read_doc` → `build_sheets`) and
`ledgr_slack` is the Slack frontend. The old eval stack graded `invoice_processing` /
`accounting_agents` with hand-coded golden answers (F/G clusters, chat B* lane).

We need an eval that:

1. Runs on the **real** `ledgr_agent` LlmAgent (not Slack, not legacy pipeline).
2. Is **reference-free** — no per-case expected extraction JSON; the source PDF is the truth.
3. Treats every invoice PDF identically (no H1/H2 category scoring).

## Decision

### Location

`ledgr_agent/eval/` — travels with the pure agent package.

### Metrics (all reference-free)

| Metric | Type |
|--------|------|
| `rubric_based_tool_use_quality_v1` | ADK built-in — `read_doc` then `build_sheets` |
| `hallucinations_v1` | ADK built-in — grounded in PDF + tool outputs |
| `extraction_self_consistency` | Custom deterministic — line sums, subtotal + tax = grand total |
| `extraction_faithfulness` | Custom async LLM judge — reads source PDF + extraction at grade time |

### Dataset

- `ledgr_agent/eval/datasets/ledgr_light_cases.json` — agents-cli (`prompt` + `inline_data` only).
- `ledgr_agent/eval/datasets/ledgr_light.evalset.json` — ADK `AgentEvaluator` (pytest `-m eval`).
- Synthetic fictional PDFs under `ledgr_agent/eval/fixtures/pdfs/`.
- Real client PDFs (e.g. Starhub) via `LEDGR_TEST_DOC_DIR` locally only — never committed.

### Flywheel

```bash
./scripts/ledgr_eval_light.sh
./scripts/ledgr_eval_light.sh compare artifacts/grade_results/results_<ts>.json
```

### CI

Scheduled + manual **live** job (`.github/workflows/eval.yml`). No hermetic/no-cred PR gate.
Default `pytest` ignores `ledgr_agent/eval/`; run deliberately with `pytest ledgr_agent/eval/ -m eval`.

### Frontend boundary

`workbook_to_ledger_payload` is tested in `tests/ledgr_slack/test_delivery.py`, not in the agent eval.

## Consequences

- Legacy F/G/B eval files retired.
- Chat lane eval archived with `accounting_agents/chat_eval`.
- Agent quality is measured by faithfulness + arithmetic, not memorized goldens.
