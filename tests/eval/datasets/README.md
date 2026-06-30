# Eval datasets

Synthetic invoice PDFs and case JSON live under **`ledgr_agent/eval/`**:

| File | Purpose |
|------|---------|
| `ledgr_agent/eval/datasets/ledgr_light_cases.json` | agents-cli (`prompt` + `inline_data`, no golden) |
| `ledgr_agent/eval/datasets/ledgr_light.evalset.json` | ADK `AgentEvaluator` / pytest `-m eval` |
| `ledgr_agent/eval/fixtures/pdfs/` | Committed fictional invoice PDFs |

Regenerate fixtures after editing PDF layouts:

```bash
uv run python ledgr_agent/eval/build_cases.py
```

Real client PDFs: set `LEDGR_TEST_DOC_DIR` locally only — never commit. See
[`anonymisation-note.md`](anonymisation-note.md).
