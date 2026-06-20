# 0015 — Eval-driven prompt loop (no hardcoded rules, no PII)

- **Status:** Accepted 2026-06-17
- **Date:** 2026-06-17
- **Deciders:** Ledgr team
- **Supersedes in part:** ADR-0014 (principle-of-no-hardcode), ADR-0011
  (prompt-engineering layer for the Understand call)

## Context

The 2026-06-17 production run exposed a class of failures the team
calls "plumbing drift": hand-coded `if doc_kind == "expense_claim"`
heuristics and per-line tax-label lookups kept getting bypassed by
documents that didn't match the rule's assumed shape. The model was
told `tax_visible_on_document` is true whenever a "Tax" / "GST" column
exists, and it false-positived on a "Currency / Amount" column,
producing `SR` on an expense claim with no tax.

Two structural problems were identified:

1. **Plumbing, not intelligence.** The Python layer was making domain
   decisions (route, tax type, header mapping) using string matches
   and `if` branches. New shapes broke the assumptions; the fixes
   were patches on patches.
2. **No measurement.** There was no eval gate that measured whether
   the routing / tax / header decisions were correct against a
   known set of cases. Regressions were caught by users in Slack,
   not by CI.

## Decision

Adopt an **eval-driven prompt loop** for the extraction-pipeline
intelligence:

| Principle | Where it lives |
|---|---|
| **No hardcoded domain rules in Python.** Direction, tax visibility, and tax type are decided by the model, the schema, and the tax classifier — not by per-shape `if` branches in the lane. | `invoice_processing/extract/ledger_extract.py` (schema + principle prompt), `invoice_processing/export/tax_classifier.py` (decision table, not `if doc_kind == ...`) |
| **Schema is the steering surface.** Field-level rules live in Pydantic `description=`, not in the prompt body. The prompt teaches the model *how to read* the document; the schema teaches it *what each field means*. | Same |
| **Three custom metrics, all >= 0.9.** The CI gate fails the build if any case scores below 0.9 on any metric. | `tests/eval/custom_metrics.py` |
| **Rubric LLM-as-judge on top of the custom metrics.** Reference-free quality via `rubric_based_final_response_quality_v1` (4 rubrics). | `tests/eval/eval_config_adr0015.yaml` |
| **Eval loop iterates the prompt, not the code.** `scripts/eval_loop.py` reads the F-cluster gate, asks an optimizer LLM to propose a rewrite, re-evaluates, stops at 0.9. | `scripts/eval_loop.py` |
| **No PII in code, prompts, or fixtures.** Anonymised placeholders (`Company-A`, `Company-B`, `Person-1`) only. CI guard fails the build on real names. | `scripts/check_no_pii.sh` |

### SG GST decision table — the single source of truth

The Python in `tax_classifier.py` already encodes every row below.
No new `if` statements are added; the model learns the table from
the prompt and emits `direction_for_client` + `tax_visible_on_document`
accordingly. The classifier then projects to the canonical
`*TaxType` codes.

| Client GST status | Direction | Tax visible on doc? | Per-line `*TaxType` | Where this is decided |
|---|---|---|---|---|
| Not registered | purchase | any | NT (input GST absorbed into cost) | `tax_classifier._classify_purchase` master gate |
| Not registered | sales | any | NT (cannot charge GST) | `tax_classifier._classify_sales` master gate |
| Registered | purchase | no | NT | `tax_visible_on_document is False` short-circuit |
| Registered | purchase | yes, single GST | SR | `_classify_purchase` rule 2 |
| Registered | purchase | yes, mixed (telco split: 9% local + 0% IDD) | split rows -> SR + ZR | `_classify_purchase` ZR rule 1 + SR rule 2 |
| Registered | purchase | exempt-supply signal | ES | `_classify_purchase` rule 3 |
| Registered | purchase | overseas supplier, no GST | OS (review for reverse charge) | `_classify_purchase` rule 4 (F6) |
| Registered | sales | no | NT (silent — user-confirmed 2026-06-17) | `tax_visible_on_document is False` short-circuit |
| Registered | sales | yes | SR (default local supply) | `_classify_sales` rule 1 |
| Registered | sales | export / international service / overseas customer | ZR | `_classify_sales` rule 2 |

### Schema additions (this ADR)

`DocumentLedgerExtract` gained two new fields and richer
descriptions on three existing ones:

- `doc_kind: Literal["invoice", "receipt", "expense_claim",
  "credit_note", "other"]` — surfaces model reasoning for rubrics
  + Slack UX. Python never switches on this value.
- `claimant_name: Optional[str]` — populated for `expense_claim`
  documents. The mapper in `ledger_extract_to_extracted_invoice`
  prefers `claimant_name` for the issuer slot when `doc_kind ==
  "expense_claim"` (data-shape plumbing, not a domain rule).
- `direction_for_client` — `description=` rewritten in
  principle-mode (money flow, not letterhead).
- `tax_visible_on_document` — `description=` rewritten to forbid
  false-positiving on `Total` / `Amount` / `Subtotal` / `Currency`
  columns.
- `direction_reason: Optional[str]` — short grounded reason
  (visible signal) the model used to decide direction. Used by
  eval rubrics for debugging; never Python-switched.
- `currency` — `description=` rewritten to direct the model to read
  the document's Currency column / total footer, not the client's
  home currency. Foreign codes in Details text (e.g. `BHT 4466.68 x
  0.0295`) are conversion notes, not the document currency. The
  2026-06-17 USD expense claim bug — every line USD, total `TOTAL
  USD 1195.11`, yet Slack showed SGD — was the trigger; the eval
  gate now fails on a regression.

### F-cluster eval set

`tests/eval/datasets/ledgr.evalset.json` grew from 13 to 25 cases
(added cluster F1..F12). All party names are anonymised. The cluster
covers the full SG-GST decision table:

- F1, F2: expense_claim (no tax + overseas receipts) → NT
- F3, F4: invoice_sales / invoice_purchase, single GST line → SR
- F5: invoice_purchase, telco split → SR + ZR
- F6: invoice_purchase, overseas supplier, no GST → OS
- F7, F8: GST-registered client, no tax on doc → NT
- F9, F10: non-GST client, GST shown → NT (master gate wins)
- F11: credit_note_sales → SR
- F12: ambiguous doc → `direction_for_client = "unknown"`
  (HITL gate fires)

### Custom metrics

`tests/eval/custom_metrics.py` exposes four ADK Custom Metrics:

- `sheet_routing_score` — 1.0 when each row's
  `direction_for_client` lands on the expected Purchase / Sales
  sheet.
- `header_mapping_score` — 1.0 when the Xero / QBS exporter row
  dict keys cover the expected column set (purchase sheet uses
  `Description`, sales sheet uses `*Description`; the metric
  accepts either).
- `tax_type_routing_score` — 1.0 when per-line `*TaxType` matches
  the SG-GST decision table for the case.
- `currency_routing_score` — 1.0 when the Xero / QBS exporter row
  carries the case-expected `Currency` (e.g. `USD` for the F1
  expense claim, `SGD` for a local trade invoice, `USD` for the
  F6 overseas vendor). Reads the user-visible Xero `Currency`
  column and falls back to `inv.currency`. Added after the
  2026-06-17 SGD-by-default bug on a USD expense claim.

All four are driven by the per-case `_F_CASE_TABLE` so the table
cannot drift between the offline pytest gate and the agent-eval
path.

### Eval loop

`scripts/eval_loop.py` runs the F-cluster gate, and — if a
credentialed LLM is available — asks the optimizer model to propose
a new system prompt. Stops at 0.9 or `max_iterations` (default 5).
The optimised prompt is **not** auto-applied; the human reviewer
pastes the printed text into `_build_understand_prompt` by hand.
The same execute-evaluate-critique-rewrite loop ADK
`SimplePromptOptimizer` runs, applied to a single `generate_content`
call rather than an `Agent`.

### CI gate

`.github/workflows/eval.yml` runs the F-cluster pytest gate on
every PR that touches `invoice_processing/extract/**`,
`invoice_processing/export/**`, `tests/eval/**`, or
`scripts/eval_loop.py`. Below 0.9 on any metric → build fails. The
PII guard (`scripts/check_no_pii.sh`) runs in the same job and
fails on any real client / vendor name outside the documented
allowlist.

## Consequences

- New document shapes no longer require Python changes — they
  require (a) a fixture in the F-cluster and (b) a prompt update
  informed by the eval loop.
- The eval gate is the contract between the prompt team and the
  export team. If the gate is green, the export layer is
  guaranteed to receive well-formed `direction_for_client` +
  `tax_visible_on_document` signals.
- Real client / vendor names never enter source control, prompts,
  or fixtures. The PII guard keeps it that way.
- The optimizer loop is offline (not on the upload hot path). It
  re-runs only when a PR changes the prompt or the schema.
- The existing B-cluster chat probe (Company-A / Person-1) stays
  intact — it's a separate lane; no PII guard allowlist is needed
  because all party names are now generic.

## References

- ADR-0011 (Understand layer)
- ADR-0014 (Capture → Book → Verify — opt-in; export principles kept)
- `invoice_processing/extract/ledger_extract.py`
- `invoice_processing/export/tax_classifier.py`
- `tests/eval/custom_metrics.py`
- `tests/eval/eval_config_adr0015.yaml`
- `tests/eval/test_f_extract_direction.py`
- `scripts/eval_loop.py`
- `scripts/check_no_pii.sh`
- `.github/workflows/eval.yml`
- Google ADK docs: <https://adk.dev/evaluate/criteria/index.md>,
  <https://adk.dev/optimize/index.md>
