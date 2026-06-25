# Clean Agent — Finish, De-False-Green, and Cutover

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development`
> (or `superpowers:executing-plans`) to implement task-by-task. Steps use `- [ ]` tracking.

**Goal:** Turn the *scaffolded* clean `ledgr_agent` (built in
[2026-06-24-clean-adk-accountant-agent-implementation.md](2026-06-24-clean-adk-accountant-agent-implementation.md),
Plans 1–6 marked "Done") into a **verified, eval-gated, live** agent, then retire the
`accounting_agents` graph. This plan closes the false-green gaps found in QA
(`ultraqa-clean-agent-holes-2026-06-24`) and performs the cutover.

**Governing decision:** [ADR-0026](../../adr/0026-ai-reads-rules-apply-on-a-lean-llmagent.md)
— lean `LlmAgent` + deterministic-engine-as-tool; AI reads (`response_schema`), deterministic
Python applies tax/COA, YAML holds rule-data, `after_tool` guards; `invoice_processing` is
KEPT, only the `accounting_agents` graph/`nodes.py` is retired.

**Architecture (unchanged from ADR-0026):**
`ledgr_agent` (LlmAgent) → `process_document_batch` tool → `invoice_processing.*` engine.
HITL via the existing `hitl.py` Firestore bridge driven by the tool's `pending_reviews`
(NOT an ADK `RequestInput` node, NOT Tool Confirmation).

**Safety rule:** Do not change `app/main.py`, the Dockerfile entrypoint, or live Slack
traffic until Stream D. Do not commit unless the user explicitly asks. Flip
`LEDGR_USE_CLEAN_AGENT` only after Stream 0 is green (D.6).

**Critical-path order:** Stream 0 → A / B / C (engine, benefits both agents) → D (cutover).
Do not start D until Stream 0 is green.

---

## Progress

| Stream | Status | Gate |
|--------|--------|------|
| 0 — Golden eval + de-false-green | ✅ GREEN (0.3 + 0.4 done 2026-06-25) | tool actually invoked (live `function_calls=[process_document_batch]`); field-match scorer (54 tests) + de-false-green lock (22 tests); batch asserted non-null; live grade non-vacuous |
| A — Read layer (regex → structured output) | ✅ A.1+A.2 DONE (2026-06-25); A.3 open | read layer already structured (kept); SST/GST+totals now `required` + described; doc_type enum-enforced; A.3 SOA fan-out deferred |
| B — Rule-data → YAML | ◐ B.1+B.2 DONE (2026-06-25); B.3 left | SG+AutoCount/SQL tax codes non-blank ✅; SR bug already-fixed ✅; ERP YAML port (B.3) outstanding |
| C — Guard (after_tool) | ✅ DONE (2026-06-25, reviewed) | Plan-4 validators fail-loud: inline surfaces errors + invalid_tax_code; after_tool callback guards the boundary (STRICT raises); 20 tests |
| D — Cutover + retirement | ☐ Not started (blocked on 0) | credits real; Slack delivery wired; HITL re-homed; flag flipped; graph deleted |

---

## Stream 0 — Golden eval set + fix the false-green *(do first; gates everything)*

**Files:** `tests/eval/datasets/`, `tests/eval/` scorer, reuse `tests/eval/test_eval_golden.py` pattern.

- [ ] **0.1** Select ~12–15 real docs from `~/Desktop/LocalTest/TestDoc/` covering SG-GST, MY-SST, bank, `multi receipt.pdf`, and ≥3 known-failing. Record the manifest (paths) in the repo.
- [ ] **0.2** *(human — David)* Author golden JSON per doc: `{doc_type, vendor, currency, total, tax_amount, lines:[{tax_code, coa_code}]}`.
- [x] **0.3** Write a **deterministic field-match scorer** — **DONE 2026-06-25** (`ledgr_agent/metrics/golden_field_match.py` + `tests/ledgr_agent/test_golden_field_match.py`, 54 hermetic tests; suite 117 green). Consumes golden v2 (`file_expectations` + `documents`). Metrics: `score_doc_count` + `score_credits` (numeric gates), `score_classification`, `score_fields`, `score_tax_coa` per-ERP (autocount/sql_account, `BLANK(hole B.1)` sentinel), `score_creditor`. N/A (score=None) excluded from mean → no false-green. In-repo synthetic fixture `tests/eval/datasets/golden_v2_sample.json`; real golden via `LEDGR_GOLDEN_MANIFEST`. Reviewed (code-reviewer): fixed index-skew in `score_tax_coa`, multi-file credits→N/A, vendor/currency absent-key→N/A. **Not** LLM-as-judge. PENDING: line-level live scoring blocked on TODO(0.4) — `export_rows` carry no doc_id, so `project_batch` emits `lines:[]` (tax/coa/creditor return N/A on live until export rows are doc-tagged); then run through `google-agents-cli-eval`.
- [x] **0.4** Fix the false-green — **DONE 2026-06-25.** (1) Permanent CI lock `tests/ledgr_agent/test_eval_metrics_non_vacuous.py` (22 tests): all 8 core metrics score 0.0 on a no-tool-call / unrelated-tool trace, and a real `process_document_batch` run on committed fixture `tests/eval_invoices/invoice_8.pdf` (injected stubs, no LLM) scores success/doc_type/export non-vacuously. `tests/ledgr_agent` 139 green. (2) Fixed `core-documents.json` to reference committed fixtures verbatim so the agent actually invokes the tool. (3) Projection adapter = `project_batch` (built in 0.3). **LIVE PROOF** (`agents-cli eval generate`+`grade`, AI Studio flash-lite): both cases now `function_calls=[process_document_batch]`, `source_resolution=disk_paths`, real extraction → real spread: accounting_task_success **0.5** (case2 `needs_review`), doc_type/tax_validity/erp_export/credit/hitl/no_unneeded **1.0**, cost_efficiency **0.0** (`llm_call_count=3>2` — confirms known **D.5** cost-telemetry over-count LIVE). Pre-0.4 these were vacuous. State-dependent datasets (`multi-erp.json` per-ERP profile, `credits.json` firm_id/dedup) flagged in README as NOT stateless-generate satisfiable (need bridge/pytest) — follow-up.
- **Gate:** ✅ scorer + metrics produce non-vacuous per-metric scores on real `ledgr_agent` traces. Stream 0 GREEN.

## Stream A — Read layer (replace remaining reading-regex with AI)

**Files:** `invoice_processing/classify/document_classifier.py`, `invoice_processing/extract/invoice_extractor.py`, `invoice_processing/export/categorizer.py`.

- [x] **A.1** **DONE 2026-06-25.** Audit found the read layer is ALREADY structured-output (not regex): `document_classifier.py` already classifies via `response_schema=ClassificationResult`; `categorizer.py` is already deterministic-first (§9 cost guardrail) + LLM fallback — both KEPT. Hardening applied: `ClassificationResult.doc_type` is now a server-enforced `Literal` enum (= `ALLOWED_DOC_TYPES`); post-LLM clamp + `free_type` preserved.
- [x] **A.2** **DONE 2026-06-25.** Tightened `ExtractedInvoice`: `gst_total`/`subtotal`/`total` now **required** `float` with precise descriptions ("never omit a printed SST/GST total; 0.0 only when no tax shown") — the under-capture fix; `issuer_tax_system` now required `Literal["GST","SST","VAT","NONE"]`. Country fields deliberately LEFT optional (unbounded → no closed enum). Ripple: 44 constructor sites across 13 test files updated + `ledger_extract.py` fallback. New `tests/test_extraction_schema.py` (16 assertions) locks the contract. Also fixed a pre-existing stale assertion in `test_eval_routing.py` (`document_workflow`→`root_accountant_agent`, ADR-0026 cutover). Full suite **~2180 passed, 0 failed**.
- **Note — A.3 (derived, OPEN):** teach extraction to fan out SOA *summary rows* (each listed invoice row = 1 doc); ATOM(14)/Auto Lab(6) remain known-failing golden cases until this lands. Bigger engine change, deferred.
- **Gate:** ✅ schema forces SST/GST + totals capture (server-side required); classification enum-enforced; full suite green, no regressions. (Live extraction-quality lift measurable once 0.4 line-projection lands.)

## Stream B — Rule-data → YAML (keep the apply-logic deterministic)

**Files:** `invoice_processing/shared_libraries/sg_gst.yaml`, `my_sst.yaml`, `export/tax_classifier.py`, `ledgr_agent/skills/erp_export_skill/` (new).

- [x] **B.1** **DONE 2026-06-25.** Added explicit `autocount:` + `sql_account:` blocks to `sg_gst.yaml` `code_map`, grounded in `docs/research/sg-gst-tax-codes.md` §7.2 (IRAS SG short-code convention, rate-invariant): purchase `SR→TX, ZR→ZR, ES→ES, OS→NT, IM→IM, NT→NT`; sales `SR→SR, …`. SG client on AutoCount/SQL now resolves real codes (was BLANK). Locked by `TestSgErpCodeResolution` (8 tests) in `test_tax_classifier.py`. Golden updated: SG telco `erp_codes` `BLANK(hole B.1)`→`TX`/`ZR` in both `/tmp/ledgr_golden/golden_truth.json` and `tests/eval/datasets/golden_v2_sample.json`. Suite: 160 (tax/erp/jurisdiction) + 139 (ledgr_agent) green, no regressions.
- [x] **B.2** **ALREADY DONE (pre-existing, verified 2026-06-25).** `tax_classifier.py` already has the master gate `if not inv.our_gst_registered: return "NT"` in BOTH `_classify_purchase` (line ~363) and the sales branch (line ~447); fed from `client.tax_registered` in `pipeline.py:277`. Covered by `test_tax_classifier.py` (non-reg client → NT for purchase/sales/ZR-signal/explicit-keyword, lines ~372–411). Memory note about the SR bug was stale.
- [ ] **B.3** Port `Ledgr-Agentic/ledgr-agent/app/skills/erp_export_skill/assets/*.yaml` (QBS Ledger, Xero, AutoCount, SQL Account) into `ledgr_agent/skills/` as declarative ERP column maps. *(Remaining — larger port; not golden-tested; lower priority.)*
- **Gate:** B.1/B.2 ✅ tax codes non-blank on SG+MY; SR bug closed; ERP/tax suites green. B.3 outstanding.

## Stream C — Guard (wire the dead validators)

**Files:** `ledgr_agent/callbacks/validate_output.py` (new), `ledgr_agent/policies/validators.py`, `ledgr_agent/agent.py`.

- [x] **C.1** **DONE 2026-06-25.** Two-layer fail-loud enforcement (code-reviewed, CRITICAL+HIGH+4 fixed). (a) Hardened inline `_run_policy_validators` (`document_tools.py`): policy-load + per-doc validator errors now SURFACE as hard `policy_validator_error` (no longer swallowed); new per-line `invalid_tax_code` check (taxable line `abs(gst_amount)>=0.005` with blank `tax_treatment`); validator messages preserved. (b) NEW ADK `after_tool_callback` `ledgr_agent/callbacks/validate_output.py` wired in `agent.py` — agent-boundary guard: if a hard violation (`gst_claimed_by_non_registered_client`/`policy_validator_error`/`invalid_tax_code`) is present but status not `needs_review`, it fails loud (STRICT env `LEDGR_VALIDATE_STRICT` raises; normal annotates `validation_summary.policy_enforcement="failed_open_detected"` + flips `success`→`needs_review`, never downgrades partial/error). Shared `HARD_VIOLATION_IDS` in `policies/constants.py`. Caught the real gap: `determine_batch_status` ignores policy violations. 20 callback tests + `tests/ledgr_agent` 159 green.
- **Gate:** ✅ deliberately-bad lines (GST-by-non-reg, blank-tax-code, validator-error) caught in unit tests across success/partial/error statuses + STRICT-raise.

## Stream D — Cutover + retirement *(blocked until Stream 0 is green)*

**Files:** `ledgr_agent/tools/document_tools.py`, `accounting_agents/slack_runner.py`, `accounting_agents/hitl.py`, `app/credit_service.py`, then delete `accounting_agents/agent.py` graph + `nodes.py` graph nodes.

- [ ] **D.1** Cut umbilical: remove `ledgr_agent` → `accounting_agents.agent._playground_default_context` import; promote engine privates (`_build_ledger_workbook`, etc.) to public API.
- [ ] **D.2** Make the **credit lifecycle real** (QA: total no-op — gate fail-opens on `firm_id=None`, no `.deduct()`): wire `firm_id` + deduction on delivery.
- [ ] **D.3** Finish Plan 6 Slack wiring: the `_use_clean_agent()` branch in `slack_runner.py` → actually map `BatchResult` → Slack delivery.
- [ ] **D.4** Re-home HITL: `slack_runner` pauses on the tool's `pending_reviews` via the existing `hitl.py` Firestore bridge (ADR-0026).
- [ ] **D.5** Fix cost telemetry (QA: invoice extraction uncounted, categorize over-counts) in `document_tools._build_pipeline_inject`.
- [ ] **D.6** Flip `LEDGR_USE_CLEAN_AGENT=true`, live QA, soak; then **delete** `accounting_agents/agent.py` graph + `nodes.py` graph nodes; retire the flag.
- **Gate:** Stream-0 eval green + live Slack QA pass (Plan 6's "eval + live QA pass") before the delete.

---

## Decisions & derived work (2026-06-24, golden-authoring session)

Golden ground-truth v2 authored by independent PDF analysis (16 logical docs, real client data,
held at `/tmp/ledgr_golden/golden_truth.json` — NOT in repo). Found Antigravity's machine manifest
wrong on 5/12 docs (ATOM total 2580→6315, Auto Lab 280→5783, GDEX 4.32→75.55, Yau Lee tax 0→SST-8%,
M-Premium/SC-Custom multi-invoice merges). User decisions:

- **SOA = book each listed invoice row as 1 document + 1 credit** (reverses skip-cover). → **new
  Stream-A item A.3**: teach extraction to fan out SOA *summary rows* (not only bundled full-invoice
  pages). ATOM (14) / Auto Lab (6) are **known-failing eval cases** until A.3 lands.
- **ERP = author both AutoCount + SQL renderings** (SV-8/SV, SV-6/SV, S-10/ST5…); JBI `400-x`
  creditor codes asserted on MY lines; COA is the shared dimension; SG cases assert COA+GST only.
- **B.1 confirmed**: SG AutoCount/SQL tax codes are BLANK (`sg_gst.yaml` lacks those code_map blocks).
- **Credit model** (user-confirmed): `credits = max(page_count, unique_document_count)` per file —
  1 page=1 credit (bank=all pages), but a page with multiple distinct receipts charges per unique doc
  (multi-receipt 35pg→87). Set totals **162 credits**. → **engine item D.2b**: change charge from
  `posted_count` (doc count only) to `max(pages, reconciled_docs)` in `document_tools.py` (gate already
  uses pages). OPEN: Starhub 18pg=18 credits for one bill — confirm whether multi-page single docs cap.
- **0.3 scorer** must assert: `documents_processed==N`, `credits_used==X`, ERP tax-code per ERP,
  creditor-code (MY). Extend `doc_count_score` + `_G_CASE_TABLE` in `tests/eval/extraction_metrics.py`.
  JBI Party List isn't auto-ingested by `load_client_setup` (reads `Entity_Memory` sheet) — load it manually.

## Notes

- The original plan's status table marks Plans 1–6 "Done" but several are **scaffolded, not verified** (this plan closes that). Its body only details Plans 1–2.
- `Ledgr-Agentic` (separate repo) is a reference prototype only — mine its `erp_export_skill` (B.3) and the payroll idea; do not deploy it.
