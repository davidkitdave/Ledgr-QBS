# Ledgr-QBS - Clean ADK Accountant Agent Design

Date: 2026-06-24
Status: Implemented (Plans 1-6 complete; QA pending)

## Purpose

This spec defines the cleaner target architecture for Ledgr-QBS after comparing
the current repo with the separate Ledgr-agentic ADK project and verifying the
direction against Google ADK and agents-cli guidance.

The goal is not to restart the product in a new repo. The goal is to build a
clean ADK accountant layer inside Ledgr-QBS, prove it with proper eval, and then
move Slack traffic to it gradually.

Simple rule:

> Gemini reads the document. Python checks the accounting truth. Config stores
> client-specific rules.

## Research Update From ADK / Google Docs

After checking the ADK docs MCP, Google Developer Knowledge MCP, and
`agents-cli info`, the best direction is:

- Keep a simple ADK agent package, like Ledgr-agentic's `app/agent.py` shape.
- Use `agents-cli` as the main build and eval harness for agent behavior.
- Use Python `FunctionTool`s for concrete capabilities with clear docstrings.
- Use `FunctionTool(..., require_confirmation=True)` or advanced tool
  confirmation for dangerous accountant write actions.
- Use a specialist `AgentTool` when a capability deserves its own agent brain.
- Use ADK graph/workflow patterns for deterministic, predictable business
  processes, especially where HITL/resume is needed.
- Keep deterministic business rules in code and configuration, not in prompt
  prose.

For Ledgr-QBS, this means we should not create a new product repo. We should add
a clean ADK package inside this repo and migrate traffic toward it.

## Current Problems To Fix

Ledgr-QBS already has valuable product features: Slack, human approval, client
profiles, multi-ERP export, Firestore sessions, and a strong test base. The
problem is that the behavior is spread across too many surfaces:

- Document processing and chat live as separate ADK apps.
- Slack event handling, runner logic, HITL, delivery, and write draining are
  concentrated in `accounting_agents/slack_runner.py`.
- Eval is split between agents-cli chat eval, pytest document eval, and legacy
  eval scripts.
- Some document understanding still relies on regex, keyword lists, fuzzy
  matching, or SG/SGD defaults.
- HITL can become noisy, especially when many account lines are low-confidence.
- Billing/credits are planned but not built into the runtime yet.

The target design keeps the accounting safety, but makes the agent shape easier
to understand, evaluate, and extend.

## Target Architecture

Two Cloud Run services, one coherent ADK accountant brain:

```text
Slack Adapter Cloud Run
  - Slack OAuth, events, files, buttons, Block Kit
  - No accounting decisions
  - Calls the ADK agent service

ADK Accountant Cloud Run
  - Root Accountant Agent
  - Document Processing capability
  - Accountant Chat Actions capability
  - Review / Confirmation capability
  - Credit / Billing capability
  - Future Reconciliation and Month-End capabilities
```

The Slack adapter is the front door. The ADK service is the accountant.

## Target Package Structure

Ledgr-agentic is easier to reason about because the ADK app lives in one small
folder:

```text
ledgr-agent/
  app/
    agent.py
    tools/
    pipeline/
    plugins/
  tests/eval/
```

Ledgr-QBS cannot reuse the name `app/` for the new ADK brain because `app/` is
already the current FastAPI/Slack entrypoint (`app/main.py`). Using `app/` for
both would make the project more confusing, not cleaner.

Resolved decision:

```text
Ledgr-QBS/
  ledgr_agent/
    __init__.py
    agent.py
    tools/
      document_tools.py
      chat_action_tools.py
      review_tools.py
      credit_tools.py
    workflows/
      document_workflow.py
      reconciliation_workflow.py
    schemas/
      batch_result.py
      document_extract.py
      review.py
      credit.py
    policies/
      jurisdictions/
        sg.yaml
        my.yaml
      erp/
        qbs.yaml
        xero.yaml
        autocount.yaml
        sql_account.yaml
    plugins/
    metrics/
  tests/eval/
  tests/eval_invoices/       # local private docs only, gitignored
  artifacts/grade_results/   # generated results, gitignored
```

`agents-cli-manifest.yaml` should eventually change from:

```yaml
agent_directory: "accounting_agents"
```

to:

```yaml
agent_directory: "ledgr_agent"
```

Do this only after the clean agent can run and eval locally. Until then,
`accounting_agents` remains the live runtime and `ledgr_agent` is the new ADK
entrypoint under test.

## What Gets Retired Later

If this plan works, we do not delete the old build in one big step. We slowly
move traffic and tests to the clean shape, then remove old surfaces only after
the new path is proven.

Simple picture:

```text
app/                 = front door for Cloud Run and Slack
accounting_agents/   = current old/live ADK brain and Slack driver
invoice_processing/  = accounting kitchen / engine library
ledgr_agent/         = new clean ADK accountant brain
tests/eval/          = official agent exam
eval/                = older research / local scripts
docs/                = decisions, QA notes, plans
```

Retire slowly:

- `accounting_agents/agent.py`, `accounting_agents/nodes.py`, and large parts of
  `accounting_agents/slack_runner.py` after `ledgr_agent/` passes eval and live
  QA.
- `eval/` ad-hoc scripts after their useful checks are moved into
  `tests/eval/` or normal pytest tests.
- stale docs after the new spec and implementation plan become the source of
  truth.
- `legacy/` once import checks confirm nothing depends on it.

Do not retire immediately:

- `invoice_processing/classify/`
- `invoice_processing/extract/`
- `invoice_processing/export/`
- `invoice_processing/shared_libraries/`

Those should become the stable accounting engine behind `ledgr_agent/`. Later,
if a module is duplicated or only used by old tests, we can move or delete it
after the new eval and unit tests cover the same behavior.

Retirement rule:

> Move the test first, move the traffic second, delete old code last.

## Root Accountant Agent

The root agent is the single conversational brain. It should understand user
intent and call the right capability.

Initial capabilities:

```text
RootAccountantAgent
  -> process_document_batch
  -> accountant_chat_actions
  -> request_or_apply_review
  -> credit_balance_and_charge
```

Future capabilities:

```text
RootAccountantAgent
  -> reconcile_bank_statement
  -> prepare_month_end
  -> find_missing_documents
  -> explain_client_books
```

Resolved staged decision:

1. Start with one root `Agent` in `ledgr_agent/agent.py`.
2. Expose document processing through one clear `process_document_batch`
   FunctionTool that returns `BatchResult`.
3. Keep the deterministic document pipeline behind that tool.
4. Once the contract and eval pass, promote document processing into a specialist
   `AgentTool` if it needs its own instructions, model strategy, or tool set.
5. Use ADK graph/workflow internals for predictable document steps, especially
   HITL/resume.

ADK verification showed that a graph `Workflow` is not a direct `sub_agent`, but
an agent/workflow can be wrapped as an `AgentTool`. That is useful, but we should
not put long Slack approval pauses inside an `AgentTool` until resumability is
proven in our runtime.

In plain words: first build one clean door. Then, if the room behind that door
gets big, make it its own specialist agent.

## Document Processing Capability

Document processing to multi-ERP output is one major capability.

```text
Upload documents
  -> classify document type
  -> Gemini structured document read
  -> canonical Ledgr schema
  -> client profile resolution
  -> COA / tax / FY policy
  -> review only when material risk exists
  -> ERP export
  -> BatchResult
```

The document read must use structured output, not free text:

```text
DocumentLedgerExtract
  - document type
  - direction for client
  - parties
  - invoice number and date
  - currency
  - totals
  - ledger lines
  - tax evidence
  - source page ranges
  - confidence / evidence fields
```

The output is then normalized into the canonical Ledgr schema:

```text
NormalizedInvoice
BankStatement
PostingLine
ExportBatch
BatchResult
```

## Multi-ERP Correctness

Gemini should not produce ERP-specific rows directly. Gemini reads the document.
The exporter projects canonical data into the target ERP.

ERP correctness layers:

1. Canonical schema is filled from Gemini's structured read.
2. Client profile resolves accounting software, COA, contacts, region, currency,
   and FYE.
3. ERP profile YAML maps canonical fields to target columns.
4. Exporter validates required fields before delivery.
5. Golden tests confirm real template headers and required values.

Current good patterns to keep:

- `invoice_processing/shared_libraries/erp_profiles/*.yaml`
- `invoice_processing/export/exporters.py`
- `tests/test_erp_golden_format.py`
- `tests/test_import_readiness.py`
- `tests/test_header_completeness.py`

Design rule:

> ERP-specific shape belongs in exporter profiles and tests, not in Gemini
> prompts and not in Slack message code.

## Market Policy YAML

Yes: SG/MY law, tax, and market behavior should be stored in versioned YAML
policy files, then enforced by Python validators.

This is not a special ADK tax-law rule. It is the safer accounting design:
ADK gives us agents, tools, state, eval, and workflow structure; our product
must still own the accounting policy and validation rules.

Think of it like this:

```text
Gemini = reads the receipt
YAML   = rule book for Singapore / Malaysia
Python = calculator and police officer
```

Do not ask Gemini to remember tax law from its training data. Let Gemini extract
evidence from the document, then let the policy engine apply the client's market
rules.

Recommended files:

```text
ledgr_agent/policies/jurisdictions/sg.yaml
ledgr_agent/policies/jurisdictions/my.yaml
```

Research-backed SG policy shape:

```yaml
policy_version: sg-2026-01
market: SG
currency: SGD
tax_system: GST
registration:
  client_flag: gst_registered
  effective_date_field: gst_registration_effective_from
  turnover_threshold_sgd: 1000000
  non_registered:
    allow_output_gst: false
    allow_input_tax_claim: false
rates:
  standard:
    - rate: 0.08
      effective_from: 2023-01-01
      effective_to: 2023-12-31
    - rate: 0.09
      effective_from: 2024-01-01
tax_codes:
  sales:
    - code: SR
      label: Standard-rated supply
      rate_type: standard
    - code: ZR
      label: Zero-rated supply
      rate: 0
      requires_reason: true
      requires_evidence: true
    - code: ES
      label: Exempt supply
      rate: 0
      requires_review_by_default: true
    - code: OS
      label: Out-of-scope supply
      rate: null
      requires_reason: true
  purchases:
    - code: TX
      label: Standard-rated purchase/input tax
      rate_type: standard
      requires_valid_tax_invoice: true
    - code: BL
      label: Blocked or disallowed input tax
      claimable: false
    - code: IM
      label: Import GST
      requires_import_permit: true
    - code: RC
      label: Reverse charge
      requires_client_reverse_charge_profile: true
invoice_evidence:
  full_tax_invoice_threshold_inclusive_sgd: 1000
  simplified_invoice_allowed_if_total_lte_sgd: 1000
  full_tax_invoice_required_fields:
    - words_tax_invoice
    - supplier_name
    - supplier_address
    - supplier_gst_registration_number
    - invoice_date
    - invoice_number
    - customer_name
    - customer_address
    - description
    - gst_rate
    - amount_excluding_gst
    - gst_amount
    - amount_including_gst
guards:
  no_tax_when_not_registered: true
  require_tax_invoice_for_input_tax: true
  require_reason_for_zero_rate: true
review_rules:
  - id: gst_claimed_by_non_registered_client
    severity: hard_review
  - id: gst_charged_without_supplier_gst_number
    severity: hard_review
  - id: input_tax_claim_without_valid_tax_invoice
    severity: hard_review
  - id: zero_rate_without_export_or_international_service_evidence
    severity: hard_review
  - id: rate_mismatch_for_invoice_date
    severity: hard_review
  - id: possible_blocked_input_tax
    severity: review
  - id: import_gst_without_import_permit
    severity: hard_review
```

Research-backed MY policy shape:

```yaml
policy_version: my-2026-01
market: MY
currency: MYR
tax_system: SST
registration:
  supplier_sst_number_field: supplier_sst_registration_number
  buyer_sst_number_field: buyer_sst_registration_number
  not_registered_value: NA
  no_sst_charge_when_supplier_not_registered: true
  imported_service_reverse_charge_can_apply_to_non_registrant: true
rates:
  sales_tax:
    - code: MY_ST_10
      myinvois_tax_type: "01"
      rate: 0.10
      label: Sales tax 10%
    - code: MY_ST_5
      myinvois_tax_type: "01"
      rate: 0.05
      label: Sales tax 5%
    - code: MY_ST_SPECIFIC
      myinvois_tax_type: "01"
      rate_type: specific
      requires_review: true
  service_tax:
    - code: MY_SVC_8
      myinvois_tax_type: "02"
      rate: 0.08
      effective_from: 2024-03-01
    - code: MY_SVC_6
      myinvois_tax_type: "02"
      rate: 0.06
      applies_to:
        - food_and_beverage
        - parking
        - logistics
        - telecommunications
    - code: MY_CC_RM25
      myinvois_tax_type: "02"
      fixed_amount: 25.00
      unit: card
myinvois:
  tax_types:
    sales_tax: "01"
    service_tax: "02"
    not_applicable: "06"
    exempt: "E"
  document_types:
    invoice: "01"
    credit_note: "02"
    debit_note: "03"
    refund_note: "04"
    self_billed_invoice: "11"
  classification_codes:
    import_goods: "034"
    import_services: "035"
    others: "022"
guards:
  no_tax_when_not_registered: true
  require_service_or_goods_basis: true
review_rules:
  - id: supplier_charges_sst_without_sst_number
    severity: hard_review
  - id: service_tax_rate_6_vs_8_ambiguous
    severity: review
  - id: tax_amount_does_not_match_rate
    severity: hard_review
  - id: exemption_without_reason_or_certificate
    severity: review
  - id: imported_service_detected
    severity: review
  - id: imported_goods_without_customs_form_reference
    severity: hard_review
  - id: foreign_currency_without_exchange_rate
    severity: review
  - id: sales_tax_goods_without_tariff_or_clear_goods_basis
    severity: review
```

Market policy research notes:

- SG research should be grounded in IRAS GST guidance: GST registration, GST rate
  change, tax invoice requirements, input tax conditions, export zero-rating,
  import GST, reverse charge, and blocked input tax.
- MY research should be grounded in RMCD/MySST and IRBM/MyInvois guidance: SST
  registration, sales tax rates, service tax rates, MyInvois tax type codes,
  e-Invoice fields, imported goods/services, and exemption handling.
- Final production policy must be reviewed by an accountant/tax professional
  before live tax automation.

Reference sources to check during implementation:

- SG IRAS GST basics and registration:
  `https://www.iras.gov.sg/taxes/goods-services-tax-(gst)`
- SG IRAS GST rate change:
  `https://www.iras.gov.sg/taxes/goods-services-tax-(gst)/gst-rate-change`
- SG IRAS input tax and invoicing:
  `https://www.iras.gov.sg/taxes/goods-services-tax-(gst)/claiming-gst-(input-tax)`
- SG IRAS zero-rating exports/international services:
  `https://www.iras.gov.sg/taxes/goods-services-tax-(gst)/charging-gst-(output-tax)/when-to-charge-0-gst-(zero-rate)`
- MY RMCD MySST:
  `https://mysst.customs.gov.my/`
- MY IRBM MyInvois SDK:
  `https://sdk.myinvois.hasil.gov.my/`

Policy eval cases to add:

```text
sg-policy.json
  - GST-registered client, 2024 standard-rated invoice at 9% passes
  - 2023 invoice at 8% passes
  - invoice spanning GST rate change triggers review
  - non-GST-registered client receives GST invoice: no input tax claim
  - supplier charges GST but GST number is missing: hard review
  - invoice above SGD 1,000 missing required tax invoice fields: review
  - simplified invoice below SGD 1,000 with required evidence passes
  - export invoice with export evidence maps to zero-rated
  - export invoice with only "overseas" wording triggers review
  - import GST without import permit triggers hard review
  - likely blocked input tax maps to blocked/review

my-policy.json
  - SST-registered supplier charging service tax 8% passes
  - F&B/logistics/telecom/parking service tax 6% passes when evidence is clear
  - sales tax 10% goods invoice passes when goods basis is clear
  - sales tax 5% or specific-rate goods without tariff evidence triggers review
  - supplier charges SST but no SST number triggers hard review
  - non-registered supplier with SST number NA and no SST charged passes
  - exemption code E without reason/certificate triggers review
  - imported service from foreign supplier triggers reverse-charge/self-bill review
  - imported goods without customs form reference triggers review
  - foreign currency invoice missing exchange rate triggers review
  - mixed-rate invoice preserves line-level tax details
```

What belongs in YAML:

- market identifier and default currency
- tax system name
- tax codes and effective dates
- rate tables
- client eligibility gates
- import/export policy names
- review thresholds
- policy version

What belongs in Python:

- arithmetic checks
- date-effective rate lookup
- tax registration gating
- subtotal + tax = total validation
- missing-required-field detection
- ERP import validation
- Firestore credit transactions

What belongs in Gemini:

- document facts
- invoice wording/evidence
- likely supply type from document content
- confidence and reason
- "I cannot tell" when evidence is missing

Every exported row should carry the policy version used, such as
`tax_policy_version: sg-2026-01`, so future audits can explain why a code was
chosen.

## Accountant Chat Actions

Do not call this "Ledger Q&A". It is broader than questions.

Accountant Chat Actions cover Slack messages like:

- "Change this AWS row to software expense."
- "Delete this duplicate invoice."
- "Re-read this document, the amount is wrong."
- "Remember this vendor goes to account 6100."
- "Why did you choose this tax code?"

The current code already uses the right ADK primitive for dangerous edits:

- `FunctionTool(..., require_confirmation=True)`
- `tool_context.request_confirmation(...)`

Keep that pattern. The clean design should make it part of the root accountant
agent capability set, not a separate mental model called Q&A.

Write safety rules:

- Read-only explanations can run immediately.
- Ledger edits must preview before writing.
- Human must confirm before amend/remove/re-extract.
- Tax is re-derived from client profile and policy, not from free text.
- Bank rows remain read-only until a balance-aware editor exists.
- Every write emits an audit record.

ADK confirmation caveat:

- Tool Confirmation is currently documented by ADK as experimental.
- Simple yes/no confirmation works with `FunctionTool(..., require_confirmation=True)`.
- Advanced confirmation works with `tool_context.request_confirmation(...)`.
- Remote Slack confirmations must send the matching `FunctionResponse`.
- Resume flows must preserve the original invocation id.
- The ADK docs list session-service limitations for Tool Confirmation, so we must
  verify our Slack/Firestore resume path before relying on native confirmation
  for long-running document approvals.

## HITL Noise Reduction

The screenshot from Slack showed a review thread with many repeated account
review bullets. The code path confirms the cause: `_needs_review` adds one reason
per flagged account line, and `_approval_summary` prints all reasons.

This is safe but noisy. The clean design should split review into hard stops and
soft warnings.

Hard stop review:

- totals do not reconcile
- missing invoice number or date
- sales vs purchase direction unknown
- tax jurisdiction conflict
- currency conflicts with client region
- required ERP import field missing
- export cannot be imported safely

Soft warning:

- account code is low-confidence but valid
- multiple similar lines need the same account decision
- alternative COA choices exist
- model confidence is low but arithmetic and export are valid

Soft warnings should be grouped. For example:

```text
11 lines have low-confidence account mapping.
Suggested account: 510-000 Auto Parts
Actions: Approve all / Edit mapping / Review details
```

Do not print one Slack approval bullet per line unless the user expands details.

New eval metric:

```text
hitl_noise_score
  - fails when one invoice creates many duplicate review bullets
  - passes when repeated account issues are grouped
```

## Hardcoding And Regex Reduction

No-hardcoding does not mean deleting all Python rules. It means avoiding brittle
Python "understanding" when Gemini should read the document.

Move away from:

- tax treatment decided by description keyword alone
- COA picked by substring keyword match
- sales/purchase decided by fuzzy name matching as primary logic
- client-specific invoice/account regex in shared Slack code
- silent SG/SGD defaults
- SOA or expense-package decisions made by brittle English sentinels

Keep deterministic:

- tax rate tables by date
- non-tax-registered client gate
- subtotal + tax = total verification
- FY date math
- ERP required fields
- exact client/contact master matches
- idempotency and dedupe
- confirmation yes/no safety

Authority matrix:

```text
Document type          Gemini structured enum, Python clamps unknowns
Direction              Gemini direction_for_client, HITL on unknown
Extraction fields      Gemini schema, Python validates totals
Tax treatment          Gemini evidence + Python law/rate guard
COA account            exact config match first, then Gemini COA choice
FY routing             Python date math and client FYE
ERP export             YAML profile and exporter code
Credits                Firestore transactions and delivery result
```

Regex may still be used for formats explicitly stored in client config, such as
invoice ID patterns. Shared hardcoded client-specific patterns should be removed.

## Model Strategy

Current defaults are good:

```text
LEDGR_MODEL_LITE = gemini-2.5-flash-lite
LEDGR_MODEL_STD  = gemini-2.5-flash
```

Use `gemini-2.5-flash-lite` for:

- document classification
- normal invoice/receipt structured read
- COA matching when context is clear
- low-risk accountant explanations when the structured context is already clear

Use stronger `gemini-2.5-flash` only for:

- scanned bank statements
- complex SOA bundles
- uncertain review/re-read
- high-risk accountant explanations

Every `BatchResult` should record:

- model tier used
- number of LLM calls
- retry count
- token/cost metadata when available
- whether fallback or review was used

This allows eval to compare both quality and cost.

Credit gates, page counts, dedupe checks, and Firestore ledger updates should not
use Gemini. Those are deterministic billing controls.

## Debug And Playground Visibility

The clean agent must be easy to inspect in `agents-cli playground`, `agents-cli
run -v`, and ADK web/playground.

Development loop:

```bash
agents-cli run "Process this safe fixture for client playground" -v
agents-cli playground
agents-cli eval generate --dataset tests/eval/datasets/core-documents.json
agents-cli eval grade --config tests/eval/eval_config.yaml
agents-cli eval compare artifacts/grade_results/baseline.json artifacts/grade_results/<new>.json
```

What must be visible from playground/tool traces:

- which tool was called
- which document lane was selected
- model tier used (`flash-lite` or stronger fallback)
- number of LLM calls
- estimated cost fields when available
- document type and direction
- hard review reasons
- grouped soft warnings
- credits estimated / used / remaining
- ERP target and export validation status
- policy version used, for example `sg-2026-01`
- trace or job id for Slack/live debugging

Tool responses should be structured dictionaries, not only nice prose. The final
Slack message can be friendly, but eval and debugging must read the structured
tool response.

## Cost And Performance Targets

The eval target is not only quality. A result is not good enough if it passes by
calling Gemini too many times or always using the stronger model.

Cost rules:

- Use `gemini-2.5-flash-lite` as the default model.
- Use stronger `gemini-2.5-flash` only as a fallback for complex or uncertain
  documents.
- Deduplicate files before expensive Gemini calls.
- Count bank statement pages before billing and before unnecessary extraction.
- Never call Gemini for credit gates, page counts, idempotency, FY math, ERP
  required-field checks, or subtotal/tax/total arithmetic.
- Batch files with a safe parallel worker cap, not unlimited concurrency.
- Cache or reuse client profile, COA, jurisdiction policy, and ERP profile data
  inside one batch.
- Return early for unsupported files instead of trying multiple expensive reads.

Performance fields to record in `BatchResult`:

```text
llm_call_count
models_used[]
strong_model_used
fallback_reason
elapsed_ms
documents_requested
documents_processed
documents_skipped_before_llm
estimated_cost
```

Eval should include cost/performance guards:

```text
cost_efficiency_code
  - passes when normal invoices use flash-lite and limited LLM calls

no_unneeded_llm_code
  - passes when credit gates, dedupe, page counts, and math do not call Gemini

latency_budget_code
  - passes when small fixture batches stay under the agreed local time budget
```

These metrics must be implemented in the exact format required by the selected
runner. In this repo, `agents-cli` eval configs can use inline `custom_function`
metrics. ADK `adk eval` custom metrics use importable Python functions via
`code_config`. Either format is acceptable if documented, but the metric must
read structured trace/tool output such as `BatchResult`, not Slack prose.

The first target can be conservative:

```text
normal single invoice: <= 2 LLM calls
normal clean invoice: no stronger-model fallback
small mixed batch: bounded parallel workers and no duplicate extraction
```

## Credit And Billing Capability

Ledgr-agentic has a simple local JSON credit ledger. Ledgr-QBS already has a
better production plan:

- `docs/adr/0016-credit-deduction-and-manual-topup.md`
- `docs/superpowers/plans/2026-06-20-slack-credit-system.md`

This design adopts the QBS credit plan.

Credit rules:

- Firm = Slack workspace `team_id`.
- One credit balance per firm.
- Gate before expensive processing.
- Deduct only after successful delivery.
- Deduct from actual appended/posted result, not from attempted docs.
- Deduped docs are 0 credits.
- Rejected docs are 0 credits.
- Re-extract / replace-in-place is 0 credits.
- Invoice / receipt / expense claim / other = 1 credit per unique document written.
- Bank statement = source PDF page count.
- Every grant and deduction is a Firestore transaction with an audit ledger row.

The root accountant agent should surface credit facts in `BatchResult`:

```text
credits_estimated
credits_used
credits_remaining
credit_status
credit_ledger_refs
```

Credit failure is not a document extraction failure. It is a billing gate result.

## BatchResult Contract

Every document job should return one structured result that Slack, eval, and logs
can all read.

Draft fields:

```text
BatchResult
  status
  client_id
  firm_id
  source_files[]
  per_file[]
  posted_documents[]
  skipped_documents[]
  review_requests[]
  soft_warnings[]
  erp_exports[]
  credits_estimated
  credits_used
  credits_remaining
  models_used[]
  validation_summary
  audit_refs[]
```

The important rule: eval should grade this structured result, not the final Slack
message prose.

## Proper ADK Eval Plan

`agents-cli` should be part of this build. It is not only for humans; it is also
the right tool for a coding agent to scaffold, run, evaluate, compare, and debug
an ADK agent.

Current local status:

```text
agents-cli version: 0.4.0
project name: ledgr-qbs
deployment target: cloud_run
current agent directory: accounting_agents
target agent directory: ledgr_agent
region: asia-southeast1
```

Use agents-cli as the primary behavior eval loop for the clean root accountant
agent:

```bash
agents-cli eval generate --dataset tests/eval/datasets/<suite>.json --output artifacts/traces/
agents-cli eval grade --traces artifacts/traces/<trace>.json --config tests/eval/eval_config.yaml
agents-cli eval compare <baseline>.json <candidate>.json
agents-cli eval analyze --eval-result <candidate>.json
```

Scope rule:

- Use `agents-cli` for root-agent behavior, tool choice, tool traces, chat
  actions, confirmation flows, and structured `BatchResult` grading.
- Keep pytest or ADK `AgentEvaluator` for raw PDF/document extraction tests until
  the clean `process_document_batch` contract makes those cases path/tool based.
- Do not force every current document test into `agents-cli` before the new
  contract exists.

Primary deterministic metrics:

```text
accounting_task_success_code
doc_type_code
direction_code
tax_validity_code
coa_mapping_code
erp_export_shape_code
credit_charge_code
hitl_noise_score
no_silent_default_code
reconciliation_balance_code
cost_efficiency_code
no_unneeded_llm_code
latency_budget_code
```

LLM judge metrics are advisory:

```text
final_response_quality
tool_use_quality
trajectory_quality
hallucination
safety
accountant_explanation_quality
```

Do not use LLM judge as the only proof for:

- tax codes
- account codes
- invoice totals
- ERP import shape
- credit charges
- reconciliation balances

Those are accounting facts and must be checked from structured payloads,
exported rows, or ledger entries.

Use `agents-cli eval optimize` only for prompt-only failures and only after the
manual eval loop is understood. It can be expensive and should not be the first
fix.

## Eval Datasets

Create scenario-focused datasets, following the stronger Ledgr-agentic pattern.
Do not start with a huge suite. Start with 1-2 cases, make them pass, then grow
the suite.

Before replacing production traffic, target 8 committed eval suites. This is a
Ledgr product quality gate, not a Google-mandated number:

```text
core-documents.json
  - one purchase invoice
  - one sales invoice
  - one receipt

mixed-batch.json
  - invoice + bank statement + duplicate

multi-erp.json
  - same canonical invoice exported to QBS, Xero, AutoCount, SQL Account

jurisdiction.json
  - SG GST
  - MY SST
  - cross-border
  - missing client region

hitl-review.json
  - hard stop review
  - soft grouped account warning
  - no-review happy path

credits.json
  - zero balance gate
  - delivery charge
  - dedup no charge
  - re-extract no charge

chat-actions.json
  - amend row with confirmation
  - remove row with confirmation
  - learn mapping

cost-performance.json
  - normal invoice stays on flash-lite
  - duplicate file skips before Gemini
  - zero-credit gate blocks before Gemini
  - small mixed batch stays within LLM call budget
```

Each case should include expected structured assertions in metadata, not hidden
inside prompt substring checks.

Suggested coverage target:

```text
MVP before new agent prototype: 2 suites, 4-6 cases total
Before Slack cutover: 8 suites, about 30-50 cases total
After launch hardening: 10+ suites, adding reconciliation and month-end
```

Pass target:

```text
deterministic accounting metrics >= 0.90
no critical metric below threshold
no cost/performance regression versus baseline
LLM judge metrics used as advisory, not sole proof
```

### Private Local Eval Documents

It is acceptable to use desktop/local real invoices for development and tuning,
but they must stay local.

Allowed local-only paths:

```text
tests/eval_invoices/
scratch/
playground_profile.json
artifacts/grade_results/
```

These are already gitignored in QBS or should remain gitignored. Do not commit:

- real client PDFs
- screenshots with client data
- full trace files containing document text
- generated exports from real invoices
- playground profiles with real client names

Committed eval assets should be one of:

- synthetic invoices
- redacted fixtures
- metadata-only expected assertions
- tiny fake PDFs created only for tests

Private repo does not mean "safe to commit client documents." Treat private
documents like cash in a drawer: useful while working, but not something to put
into the product box.

## Testing And Verification Gates

Every implementation slice must define a real pass condition.

### Local unit tests

```bash
uv run pytest tests/test_erp_golden_format.py tests/test_import_readiness.py tests/test_header_completeness.py -q
```

Add focused tests for:

- `BatchResult` schema validation
- hard stop vs soft warning classification
- grouped HITL summary
- no SG/SGD silent default
- credit charge rules

### Agents-cli eval

Run after any prompt/tool/schema change:

```bash
agents-cli eval generate --dataset tests/eval/datasets/core-documents.json --output artifacts/traces/
agents-cli eval grade --traces artifacts/traces/<trace>.json --config tests/eval/eval_config.yaml
agents-cli eval compare artifacts/grade_results/baseline.json artifacts/grade_results/<new>.json
```

Pass gate:

```text
all deterministic *_code metrics >= 0.90
no regression against baseline
hitl_noise_score passes
credit_charge_code passes
```

### Live Slack QA

Before production traffic moves:

1. Upload a normal invoice. Confirm no HITL.
2. Upload a low-confidence COA invoice. Confirm grouped review, not line spam.
3. Approve grouped review. Confirm workbook rows append correctly.
4. Edit one row from Slack. Confirm two-turn confirmation and audit.
5. Export to AutoCount and SQL Account. Confirm required import fields.
6. Run zero-credit upload. Confirm processing is blocked before Gemini spend.
7. Run dedup upload. Confirm no extra credit charge.

## Cleanup And Trimming Plan

Before deleting code, classify each candidate as:

```text
live
test-only
legacy-reference
safe-to-remove
```

Cleanup candidates:

- legacy eval scripts under `eval/` after their checks are moved into canonical
  agents-cli custom metrics or pytest gates
- duplicated document orchestration between `invoice_processing/pipeline.py` and
  production graph nodes
- old retired routing/coordinator remnants
- client-specific regex in shared Slack code
- unused legacy configs under `legacy/` after confirming no imports

Do not remove accounting tests just because implementation changes. Move the
assertion to the new canonical path first, then remove the old harness.

Docs cleanup candidates:

- keep this spec as the current clean-ADK source of truth
- keep ADRs for historical decisions, but add a short index that says which are
  superseded by the clean ADK design
- move old QA session notes that are not active gates into an archive folder
- update stale README sections that still describe retired flows
- make `tests/eval/README.md` the canonical eval guide and retire duplicate eval
  instructions elsewhere

Project structure cleanup target:

```text
Live app entry:        app/
Clean ADK brain:      ledgr_agent/
Accounting engine:    invoice_processing/
Current old runtime:  accounting_agents/  # shrink after cutover
Official eval:        tests/eval/
Legacy local eval:    eval/               # retire after parity
Historical docs:      docs/adr/ and archived QA notes
```

## Migration Stages

Stage 1: Contract first

- Add `BatchResult` and review/warning schemas.
- Define structured eval assertions.
- Add the clean `ledgr_agent/` package beside the current runtime.
- Keep `agents-cli-manifest.yaml` pointing at `accounting_agents` until the new
  package runs and evals locally.
- No Slack traffic change.

Stage 2: Clean root accountant prototype

- Add root accountant agent in `ledgr_agent/agent.py`.
- Wrap existing engine functions as one clean `process_document_batch` tool.
- Add jurisdiction policy YAML for SG/MY and a Python policy loader.
- Run agents-cli eval on small datasets.

Stage 3: HITL and ERP correctness improvements

- Split hard stop vs soft warning.
- Group account-review warnings.
- Add multi-ERP export shape metrics.

Stage 4: Credit integration

- Implement the approved credit plan.
- Gate before expensive processing.
- Deduct on delivery.
- Add credit eval and live QA.

Stage 5: Slack adapter split

- Move Slack-specific concerns to a separate service boundary.
- Keep accounting logic inside the ADK service.

Stage 6: Expand specialist capabilities

- Reconciliation workflow.
- Month-end workflow.
- Missing document workflow.

## Non-Goals For First Slice

- Do not rebuild every accounting step as an LLM agent.
- Do not remove human confirmation for writes.
- Do not replace tax math with an LLM judge.
- Do not build Stripe/self-serve billing.
- Do not split Cloud Run services before the root accountant contract and eval
  are proven locally.

## Open Decisions Before Implementation

Resolved:

1. The clean ADK root agent should live under `ledgr_agent/`, not `app/`.
   `app/` already serves the current Cloud Run/FastAPI Slack entrypoint.
2. Document processing should start as one clear FunctionTool façade returning
   `BatchResult`, then graduate to an `AgentTool` specialist after the contract
   and eval are stable.
3. Private desktop/local docs may be used in `tests/eval_invoices/` or
   `scratch/`, but only synthetic/redacted/metadata fixtures should be committed.
4. Normal clean invoices should auto-approve at least 90% of the time in the
   eval set. HITL should be for real risk, not normal low-drama invoices.

Still open:

1. Which synthetic or redacted invoices should become the first committed
   `core-documents.json` dataset?
2. What policy version naming should be used for SG/MY YAML files?
   Suggested: `sg-2026-01`, `my-2026-01`.

## Recommended First Implementation Slice

Build only the contract and eval shell first:

1. Create `ledgr_agent/` with `agent.py`, `tools/`, `schemas/`, `policies/`, and
   `metrics/`.
2. Define `BatchResult`, `ReviewRequest`, `SoftWarning`, and `CreditSummary`.
3. Add a thin `process_document_batch` tool wrapper around existing production
   processing.
4. Add `sg.yaml` and `my.yaml` policy files plus a Python policy loader.
5. Create one tiny agents-cli dataset with a deterministic stub or safe fixture.
6. Add deterministic custom metrics that read the tool response.
7. Keep Slack production traffic unchanged.

This gives us the new clean shape without risking the current bot.
