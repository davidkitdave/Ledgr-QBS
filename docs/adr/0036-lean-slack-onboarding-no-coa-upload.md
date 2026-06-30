# 0036 — Lean Slack onboarding: profile modal only; no COA upload on live path

- **Status:** Accepted
- **Date:** 2026-06-30
- **Deciders:** Ledgr team
- **Supersedes (in part):** ADR-0006 (COA upload paths A/B/C on the Slack frontend)
- **Relates to:** ADR-0032 (`ledgr_slack` + `ledgr_agent` split),
  ADR-0030/0031 (light read + `build_sheets`),
  ADR-0016 (credit gate and charge)

## Context

ADR-0006 defined three Slack paths to capture a client's Chart of Accounts (COA)
and a soft gate (`pending_coa` → `active`). That design matched the legacy
`invoice_processing` factory, which ran a categorizer against the uploaded COA
before export.

The live path (ADR-0032) uses `ledgr_agent` tools only:

1. `read_doc` — one Gemini read
2. `build_sheets` — deterministic ERP projection from skill YAMLs

That path never wired the legacy categorizer. COA upload UI and Firestore
`clients/{id}/coa/` storage were dead weight and confused onboarding.

Account coding on the light path will be designed in `ledgr_agent` (schema/read),
not by restoring spreadsheet COA upload.

## Decision

**1. Slack onboarding = profile modal only.**

`/ledgr settings` captures: client name, region, FYE month, accounting software,
GST registered. Saving the modal sets `status: "active"` immediately.

**2. No COA upload on the live Slack path.**

Remove: COA confirm cards, `app/coa_*` modules, Firestore COA subcollection
writes, `ledgr_slack/export/categorizer.py` on the live package, and
`pending_coa` as a production status.

**3. Document processing unchanged.**

File upload → credit gate → `read_doc` → `build_sheets` → FY ledger append.
Credits per ADR-0016 (gate before Gemini; deduct in `build_sheets`; footer on
delivery card).

**4. Legacy removed (2026-07).**

The old categorizer, graph nodes, and `invoice_processing` factory were deleted.
They are not in the repo; historical behavior is documented in ADRs before ADR-0030.

## Consequences

- Simpler onboarding: modal → drop PDFs.
- No false promise that uploading a COA spreadsheet enables account codes on
  export rows (light path leaves Account Code blank until the agent approach
  lands).
- ADR-0006 remains historical context; new work references this ADR for Slack
  onboarding scope.

## Verification

- `rg "coa_ingest|pending_coa|save_coa|_offer_coa" ledgr_slack/ app/` → empty
- Onboarding tests expect `status: "active"` without COA prompt blocks
- Credit tests still pass (`test_credit_delivery`, `test_light_path_billing`)
