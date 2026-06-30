# Architecture decision records (ADR)

## Current (live runtime)

These ADRs describe **`ledgr_slack` + `ledgr_agent`** — the only production stack.

| ADR | Topic |
|-----|--------|
| [0002](0002-slack-as-system-of-record-fy-canvas-index.md) | Slack FY workbook as system of record |
| [0004](0004-learning-via-structured-corrections-not-memory-bank.md) | Learning via structured corrections |
| [0005](0005-canonical-schema-with-per-target-projection.md) | Canonical schema + per-target projection |
| [0016](0016-credit-deduction-and-manual-topup.md) | Credit gate and charge |
| [0018](0018-cicd-github-actions-artifact-registry-cloud-run.md) | CI/CD and Cloud Run |
| [0022](0022-firestore-dev-prod-isolation-namespace.md) | Firestore dev/prod namespace |
| [0024](0024-cross-border-auto-book-escalate-only-ambiguity.md) | Cross-border tax routing |
| [0026](0026-ai-reads-rules-apply-on-a-lean-llmagent.md) | AI reads; rules apply |
| [0028](0028-accounting-module-per-erp-payment-status-universal.md) | ERP payment-status columns |
| [0030](0030-direct-call-extraction-beats-chunked-factory.md) | One Gemini read beats factory |
| [0031](0031-light-path-minimum-policy-ladder.md) | Light path policy ladder |
| [0032](0032-ledgr-agent-and-slack-two-packages.md) | **Two-package split (authoritative)** |
| [0033](0033-reference-free-ledgr-agent-eval.md) | Reference-free agent eval |
| [0034](0034-schema-as-prompt-extraction.md) | Schema-as-prompt extraction |
| [0035](0035-bookable-row-granularity-metadata-first.md) | Bookable row granularity |
| [0036](0036-lean-slack-onboarding-no-coa-upload.md) | Lean Slack onboarding |

Start here for new work: **0032**, **0030**, **0036**, **CONTEXT.md**.

## Archived

Superseded decisions for the deleted `accounting_agents` / `invoice_processing` stack live under [`archive/`](archive/). They are kept for audit trail only.

See [archive/README.md](archive/README.md).
