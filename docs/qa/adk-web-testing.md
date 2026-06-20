# ADK Web Testing — Setup, App Selection, and SG/MY Doc Matrix

How to run a structured QA session in ADK web that exercises the multi-country
build (Phase 8 / Phase 9 / Phase 1 / Phase 2 lands). One operator, two
sessions: YAU LEE Malaysia SST receipt + a clean Singapore GST invoice.

## Why this protocol

Per ADK docs (`adk.dev/sessions/state` + `adk.dev/runtime/web-interface` +
`adk.dev/graphs/dynamic`): ADK web is the ground-truth view for behavior,
not pytest on LLM text. The Graph tab is a static picture; the Traces
tab is the runtime order. State tab is the "did the agent actually see
the right context" check.

Three Apps means three test surfaces:

1. **`accounting_agents`** (full coordinator + document) — for end-to-end
   behavior QA on file uploads.
2. **`accounting_agents_document`** (document lane only) — for matching the
   production Slack path (skips coordinator LLM hop).
3. **`accounting_agents_assistant`** (chat lane only) — for chat-only QA
   (separate multi-turn session).

## Setup (one-time per machine)

```bash
cd /Users/davidkitdave/Projects/Ledgr-QBS

# 1. Env: dev (or unset — same default)
export LEDGR_ENV=dev

# 2. Playground profile: JBI PLUS / MALAYSIA / MYR + COA + YAU LEE entity memory
#    (default at repo root is the Phase 8 / multi-country seed profile)
export LEDGR_PLAYGROUND_PROFILE_PATH=playground_profile.json
# Optional: override region / currency / software inline
#   export LEDGR_PLAYGROUND_REGION=MALAYSIA
#   export LEDGR_PLAYGROUND_CURRENCY=MYR
#   export LEDGR_PLAYGROUND_SOFTWARE=qbs

# 3. Tax registration thresholds (optional, env override per region)
#   export LEDGR_TAX_REGISTRATION_THRESHOLD_MY=500000  # SST (RM500k)
#   export LEDGR_TAX_REGISTRATION_THRESHOLD_SG=1000000 # GST (SGD 1M)

# 4. Start ADK web. The CLI uses google-adk 2.x and FastAPI for the dev server.
.venv/bin/adk web accounting_agents --port 8080
```

Open `http://localhost:8080` in a browser. You should see three App
selectors at the top of the page.

### App selector decision

| Goal | App to select |
|---|---|
| End-to-end with intent classification | `accounting_agents` |
| Match production Slack upload path | `accounting_agents_document` |
| Chat only (per-thread multi-turn) | `accounting_agents_assistant` |

For doc lane QA, **`accounting_agents_document`** is the recommended
default — it matches production Slack behaviour and skips the coordinator
LLM hop that adds latency + non-determinism to the QA pass.

## Test matrix (5 docs, 2 jurisdictions, 2 Apps)

| # | Doc | Client profile | App | Must pass |
|---|---|---|---|---|
| 1 | SG purchase invoice (Acme SG, 9% GST line) | `region: SINGAPORE, base_currency: SGD` | `accounting_agents_document` | `tax_treatment: SR`, no `SR 9% mismatch` flag, `tax_jurisdiction: SINGAPORE` |
| 2 | **YAU LEE MOTOR Malaysia receipt** (8% SST line) | `playground_profile.json` (MY) | `accounting_agents_document` | `tax_treatment: SR`, **no** SG 9% flag, `tax_jurisdiction: MALAYSIA`, `account_code: 500-020` |
| 3 | MY telco bill (multi-line SR + ZR) | MY profile | `accounting_agents_document` | SST split lines, `tax_jurisdiction: MALAYSIA` |
| 4 | SG bank statement | SG profile | `accounting_agents_document` | Bank lane only (no `tax_node` runs) |
| 5 | Cross-border (SG client, MY supplier) | SG profile | `accounting_agents_document` | `tax_jurisdiction: CROSS_BORDER`, HITL approval, `tax_flagged: true` |

### Where the test docs live

* Sample SG purchase invoice — use any fixture from
  `tests/eval/datasets/basic-dataset.json` (e.g. `A1` happy path).
* YAU LEE Malaysia receipt — `scratch/yau_lee_motor_receipt.pdf` is the
  canonical fixture (was the c92951d1 session).
* MY telco bill — see `tests/test_telco_summary.py` fixtures (file paths
  embedded in the test).
* SG bank statement — see `tests/test_bank_bytes.py` / `tests/eval/datasets`.
* Cross-border fixture — see `tests/test_tax_classifier.py::_purchase`
  with `is_overseas=True` (constructs the SG-client + MY-supplier pair).

## Per-upload ADK web checklist

For each test doc, walk through these five tabs and record pass/fail:

### 1. Graph tab

- [ ] Pipeline is sequential after Track A/B (post Phase 6) — for
      pre-Phase-6 builds, the dynamic-driver star is the expected shape.
- [ ] No misleading `[NO DEFAULT]` without a documented fallback (in our
      build, all three routes have explicit destinations).
- [ ] Nested gray `document_workflow` box is still visible (Track A) or
      has been flattened into the coordinator graph (Track B).
- [ ] `resolve_jurisdiction_node` appears between `categorize_node` and
      `tax_node` in the commercial-doc lane (post Phase 8).

### 2. Traces tab (always ground truth)

- [ ] Order: `classify_node` → `extract_invoice_document_node` →
      `review_extraction_node` → `categorize_node` →
      `resolve_jurisdiction_node` → `tax_node` → `approval_gate` →
      `apply_decision_node` → `route_node` → `consolidate_node` →
      `deliver_node`.
- [ ] Latency dominated by extract + classify (expected).
- [ ] No orphan `Workflow document_workflow: cancelling 6 leftover tasks`
      warning (investigate if still present).

### 3. Events tab

- [ ] Profile seed populates `region`, `client_name`, `tax_jurisdiction`,
      `supplier_country`, `doc_type`, `direction`.
- [ ] No artifact 404 toast (`upload.pdf` flat name in dev).
- [ ] Tax event: no `SR 9% mismatch` on a valid 8% SST line.
- [ ] HITL only when genuinely ambiguous — not for jurisdiction mismatch
      that should resolve to CROSS_BORDER + flag rather than false SR.

### 4. State tab

- [ ] `region` matches the client profile (SG / MY).
- [ ] `tax_jurisdiction` written by `resolve_jurisdiction_node` (one of
      SINGAPORE / MALAYSIA / CROSS_BORDER / AMBIGUOUS).
- [ ] `tax_system_hint` present.
- [ ] `jurisdiction_rates` carries `standard_rate` + `rate_band_label`.
- [ ] `normalized_invoices[0].lines[0].tax_flagged` = false for a
      mathematically-clean invoice (YAU LEE: 4.81 ≈ 8% of 60.19).
- [ ] `account_code` populated for YAU LEE (was `""` in the c92951d1
      session because `playground_profile.json` had empty `coa[]`).

### 5. Artifacts tab

- [ ] PDF loads without 404. If it 404s, the artifact name has slashes
      — confirm `LEDGR_ENV` is `dev` (or unset) so `artifact_name_for`
      returns flat names.

## Common QA findings + fixes

| Finding | Likely cause | Fix |
|---|---|---|
| `tax_treatment: SR` flagged for `SR 9% mismatch` on a 60.19 / 4.81 line | `state["region"]` not set → resolver returns AMBIGUOUS → forced NT, but the OLD `TaxClassifier` ran before jurisdiction resolution and used SG 9% | Confirm `playground_profile.json` is loaded; confirm `resolve_jurisdiction_node` ran (check Traces tab) |
| `account_code` blank for YAU LEE | `state["coa"]` is empty list | Confirm `playground_profile.json` carries the COA array; restart ADK web after editing the JSON (the load callback reads at session start) |
| Artifact 404 for `inbox/upload.pdf` | `LEDGR_ENV=prod` set in dev session | Unset `LEDGR_ENV` (or set to `dev`); restart ADK web |
| `tax_jurisdiction: AMBIGUOUS` when client is clearly SG / MY | Region or base_currency missing from state | Add the missing key to `playground_profile.json` or to the on-agent client profile loader |
| `tax_node` not running for the bank lane | classify_node emitted `route=bank_statement` and the lane registry routed to bank lane only | Correct behaviour — no action needed |
| Three Apps visible in dropdown | Intentional — `app` (full), `document_app` (no coordinator), `assistant_app` (chat) | Confirm the test is selecting `document_app` for doc QA |
| `[NO DEFAULT]` on coordinator router | Hard-coded by design — all three routes have explicit edges | Confirm `dynamic_router` emits one of the three route labels |

## Logging the session

Write session results to `docs/qa/adk-web-session-YYYY-MM-DD.md` with:

1. Test doc name + fixture path.
2. App selected.
3. Per-tab pass/fail for the checklist above.
4. Any unexpected events or warnings.
5. Recommended follow-up actions (new eval cases, graph changes,
   threshold env overrides, etc.).

Feed failures back into eval cases (`agents-cli eval generate` + `grade`).

## What NOT to do

- Do NOT merge chat into the document graph (ADR-0008; ADK rejects
  `mode='chat'` agents reached from a preceding graph node).
- Do NOT delete `document_app` — production Slack depends on it.
- Do NOT trust the Graph tab for execution order — Traces is ground truth.
- Do NOT silently fall back to SG tax rules when the client is MY —
  always check `state["tax_jurisdiction"]` in the State tab.