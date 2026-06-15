# Evaluation Datasets

This directory contains evaluation datasets for testing agent behaviour.

## Running the Golden Eval Set (Step 1.5b gate)

The primary eval file is **`ledgr.evalset.json`** — the gate for master-plan
Step 2 (extract reviewer). It requires live Gemini credentials
(`GOOGLE_API_KEY` or `GOOGLE_CLOUD_PROJECT`). Without credentials the tests
are **skipped**, not failed.

```bash
# Run all golden eval cases (gated marker — opt-in only):
pytest tests/eval/ -m eval -v

# Run a single case by parametrised ID:
pytest "tests/eval/test_eval_golden.py::test_golden_eval_case[B3_chat_show_client_profile_trajectory]" -m eval -v

# Run the full-set convenience test (one evaluate() call across all cases):
pytest tests/eval/test_eval_golden.py::test_golden_eval_full_set -m eval -v
```

The default `pytest tests/` run ignores `tests/eval/` entirely via
`addopts = "--ignore=tests/eval"` in `pyproject.toml`, so the 886-test fast
suite is unaffected. The `@pytest.mark.eval` marker is belt-and-suspenders.

## Running via agents-cli

```bash
# Generate traces (runs the agent against each case):
agents-cli eval generate \
  --dataset tests/eval/datasets/ledgr.evalset.json \
  --output tests/eval/.traces/

# Grade the traces:
agents-cli eval grade \
  --metrics tool_trajectory_avg_score,response_match_score \
  --traces tests/eval/.traces/

# Compare two grade-result files (regression check):
agents-cli eval compare baseline.json candidate.json

# Discover all available built-in metrics:
agents-cli eval metric list
```

## Where Source Documents Live

Raw PDFs and XLSX workbooks are on the **developer's local machine only** —
never committed to the repo. The evalset carries only expected output values
and path references (see `anonymisation-note.md`).

| Cluster | Local path |
|---------|-----------|
| Test invoices | `${LEDGR_TEST_DOC_DIR}/invoices/Purchase/FY2026/` |
| Test bank statements | `${LEDGR_TEST_BANK_DIR}/` |
| Client Manager (profile) | `${LEDGR_TEST_DOC_DIR}/client-manager.xlsx` |

Ground-truth workbook for bank statement totals:
- `${LEDGR_TEST_BANK_DIR}/bank-statement-fy2025.xlsx`

Developers map these env vars to their local test-firm folder (see private memory `cast-unity-test-data` for the concrete mapping on the developer's machine). Real client/firm names never appear in committed files (per project rule `no-real-client-data-in-repo`).

## Eval Case Clusters

| ID prefix | Cluster | Gate purpose |
|-----------|---------|-------------|
| A1–A2 | Happy-path doc lane | Engine baseline: classify + extract |
| B3–B5 | Chat trajectory | `assistant_agent` tool-call trajectory + silence guard |
| C6–C8 | SG GST rule regression | NT / SR / ZR per `tax_registered` flag |
| D9 | HITL Edit round-trip | `apply_decision_node` writes `tax_treatment`/`net_amount` (not legacy names) |
| E10 | Adversarial | Unreadable doc flagged, not silently written |

## Adding a New Case

1. Add a new object to `eval_cases` in `ledgr.evalset.json` with a unique `eval_id`.
2. Fill `session_input.state` with the minimum profile + ledger rows the case needs.
3. Write one `Invocation` per turn in `conversation`:
   - `user_content` — the user's message (`role: "user"`).
   - `final_response` — a short anchor string for the evaluator (`role: "model"`).
   - `intermediate_data.tool_uses` — list of `{"name": ..., "args": {...}}` for trajectory assertions. Leave `[]` when no tool call is expected.
4. For doc-lane cases referencing a real file, use an anonymised firm name in text fields and carry the local path only in `user_content.parts[0].text`.
5. Add the `eval_id` to `_CASE_IDS` in `tests/eval/test_eval_golden.py`.
6. Run `pytest tests/eval/ -m eval -v` to verify the new case passes.

## ADK EvalSet Schema Reference

The evalset uses the Pydantic-backed ADK schema:

- `EvalSet` / `EvalCase` / `Invocation` / `IntermediateData` / `SessionInput`
- Installed source: `.venv/lib/python3.12/site-packages/google/adk/evaluation/eval_case.py`
- Installed source: `.venv/lib/python3.12/site-packages/google/adk/evaluation/eval_set.py`
- Docs: https://adk.dev/evaluate/index.md
- Criteria reference: https://adk.dev/evaluate/criteria/index.md

Minimal single-turn case shape:

```json
{
  "eval_set_id": "my_set",
  "eval_cases": [
    {
      "eval_id": "my_case_id",
      "session_input": {
        "app_name": "accounting_agents",
        "user_id": "eval_user",
        "state": {"client_name": "Test Firm"}
      },
      "conversation": [
        {
          "invocation_id": "inv-01",
          "user_content": {"role": "user", "parts": [{"text": "who am I working with?"}]},
          "final_response": {"role": "model", "parts": [{"text": "Test Firm"}]},
          "intermediate_data": {
            "tool_uses": [{"name": "show_client_profile", "args": {}}],
            "intermediate_responses": []
          }
        }
      ]
    }
  ]
}
```

## Beyond Generate and Grade

- `agents-cli eval compare BASE CAND` — diff two grade-result files (regression check).
- `agents-cli eval analyze RESULTS` — cluster failure modes.
- `agents-cli eval optimize` — auto-tune agent prompts using eval data.

See https://google.github.io/agents-cli/guide/evaluation/ for the full surface.
