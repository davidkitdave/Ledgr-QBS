# WS-0.3 — Vertex Flash-Lite enum-in-nested-array spike

**Date:** 2026-06-21  
**Task:** Replicate research §9 Spike A on **Vertex** (prod backend path), not AI Studio Flash.  
**Script:** `scripts/spike_vertex_enum_nested_array.py`

## Setup

| Parameter | Value |
|-----------|-------|
| Backend | Vertex AI (`GOOGLE_GENAI_USE_VERTEXAI=TRUE`, ADC) |
| Project | `ledgr-qbs` |
| Location (Flash-Lite) | `us-central1` — **required** for `gemini-2.5-flash-lite` (404 in `asia-southeast1`) |
| Model | `gemini-2.5-flash-lite` via `lite_model()` |
| Schema | `Doc{ lines: list[Line{ description, account_code: ENUM[N] }] }` — raw JSON schema with per-line `enum` |
| Enum keys | **158 synthetic** codes `100-001` … `100-158` (no real client COA) |
| Adversarial lines (6) | office rent, staff salary, unicorn transport reimbursement, cloud hosting, motor parts supply, unknown miscellaneous |
| Runs | 18 |
| Temperature | 0 |
| Total line checks | 18 × 6 = **108** |

### Reproduce

```bash
# Full acceptance run (Flash-Lite on Vertex)
LEDGR_MODEL_LITE=gemini-2.5-flash-lite LOCATION=us-central1 \
  uv run python scripts/spike_vertex_enum_nested_array.py --runs 18 --enum-size 158
```

Quick smoke (24 keys, 1 run):

```bash
LEDGR_MODEL_LITE=gemini-2.5-flash-lite LOCATION=us-central1 \
  uv run python scripts/spike_vertex_enum_nested_array.py --runs 1 --enum-size 24
```

### Prod region note

Ledgr prod runs Vertex in **`asia-southeast1`** for PDPA / data residency. **`gemini-2.5-flash-lite` is not served there** (404). Prod currently maps the LITE tier to `gemini-2.5-flash` in that region (`scripts/deploy-prod.sh`, `.env` `LEDGR_MODEL_LITE=gemini-2.5-flash`). This spike validates the **Flash-Lite model id on Vertex**; enum behaviour on prod-region Flash was not re-run here (AI Studio Spike A already covered Flash; mechanism is API-config-level).

## Result

| Metric | Value |
|--------|-------|
| Out-of-set emissions | **0 / 108** |
| API errors | 0 |
| Elapsed | ~21 s |
| Spike summary | **PASS — STRUCTURAL CONFIRMED** |

Sample run output:

```
=== WS-0.3 Vertex enum-in-nested-array spike: PASS ===
Out-of-set emissions: 0 / 108
Decision: STRUCTURAL CONFIRMED
```

### Blocked-without-creds behaviour

If `GOOGLE_CLOUD_PROJECT` / ADC is missing, the script exits with code 2 and prints `BLOCKED: …`. No live result is recorded in that case — re-run when creds are available.

## Decision

**CONFIRMED STRUCTURAL** on Vertex `gemini-2.5-flash-lite`: enum constraint in a nested `lines[]` array holds at 158 keys — **0 out-of-set emissions** under adversarial pressure, matching AI Studio Spike A (§9).

### Impact on WS-3 COA design

- **No change to the enum-as-structural-gate design.** Post-validation of code *membership* remains a semantic-plausibility check only; structural validity is guaranteed by constrained decoding on Vertex Flash-Lite.
- **UNMAPPED sentinel requirement unchanged.** Under temp 0 the model still picks an in-set code for nonsense lines (e.g. unicorn transport) rather than abstaining — confirmed by behaviour in this spike’s responses. WS-3 must keep **`account_code: enum(<client codes> + "UNMAPPED")`** (or nullable) so “no fit” → blank+flag+HITL, not a forced wrong-but-valid code.
- **Region/model routing stays a separate concern:** COA enum schema is valid on Flash-Lite@Vertex; prod’s asia-southeast1 Flash substitution does not invalidate this finding but should be tracked if Flash-Lite is ever required in-region.

## Cross-reference

- Research §9 Spike A (AI Studio Flash): 0/108 — baseline
- Plan WS-0.3 acceptance gate: **met**
- Plan WS-3.1 (`UNMAPPED` sentinel): **still required**
