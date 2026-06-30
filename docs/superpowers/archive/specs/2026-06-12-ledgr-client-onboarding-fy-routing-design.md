# Ledgr — Client Onboarding, Profile & FY Routing (design spec)

**Date:** 2026-06-12
**Status:** Approved (brainstorm). Supersedes the legacy `Sys_Config` sheet approach for client config.

## Context
The client profile is **not** read from a `Sys_Config` sheet (that was the legacy Google-Sheets
mechanism — dropped). Instead, each client's profile is created **per Slack channel** (one channel =
one client) through a Slack onboarding flow and stored in **our own datastore (Firestore)**. The client's
COA / Category_Mapping / Entity_Memory are client-provided/learned data stored alongside the profile.
The financial year is set at profile creation and drives where each extracted document is routed.

This spec covers four things: the client-profile data model (Firestore), the Slack onboarding UX, the
financial-year model, and document routing (archive + workbook). It feeds plan task **#11** (per-client
datastore + onboarding) and connects to the already-built **#12** categorizer.

## Decisions locked (this brainstorm)
1. Datastore = **Firestore** (already enabled on GCP project `ledgr-qbs`).
2. Onboarding modal captures **only 4 fields**: client company name, FYE month, accounting software, GST-registered.
3. Financial year is **FYE-month driven** (given directly; no incorporation-date derivation).
4. Routing does **both**: archive the source PDF in GCS by client/FY/doc-type **and** append rows to that FY's consolidated workbook.

## 1. Client profile — data model (Firestore)
One profile document per client, bound to the Slack channel.

```
clients/{client_id}
  client_id            : str   (generated, stable)
  channel_id           : str   (Slack channel — one channel = one client)
  slack_team_id        : str   (workspace)
  client_name          : str
  fye_month            : int   (1–12; FYE = last day of this month)
  accounting_software  : str   ("QBS Ledger" | "Xero")
  gst_registered       : bool
  region               : str   (default "SINGAPORE"; MY later)
  base_currency        : str   (default "SGD")
  status               : str   ("pending_coa" | "active")
  created_at, updated_at
  category_mapping     : map   { category -> account_code | null }   (seeded from standard universal categories)
  # subcollections:
  coa/{n}              : { code, description, account_type, financial_statement, nature, keywords }
  entity_memory/{n}    : { name, reg_no, mapping_code, role, tax_code }   (grows from ✏️ corrections)

channels/{channel_id}  : { client_id }          # reverse index: channel -> client
workspaces/{team_id}   : { bot_token, ... }      # OAuth/bot tokens (later, task #5)
```

Region and base_currency are **not** asked at onboarding (SG-first defaults); a region field is added when
Malaysia is enabled. COA/Category_Mapping/Entity_Memory are client data, not profile config — see §2.

## 2. Onboarding UX (auto-greet + 4-field modal)
1. **Discovery:** when the bot is added to a (client) channel, it posts a welcome card with a **“Set up this client”** button.
2. **Modal** (Block Kit) — exactly 4 inputs:
   - Client company name — `plain_text_input`
   - FYE month — `static_select` (January … December)
   - Accounting software — `static_select` (QBS Ledger, Xero)
   - GST-registered — a checkbox/toggle (`checkboxes` or radio Yes/No)
3. **Submit** → write `clients/{client_id}` + `channels/{channel_id}` (status `pending_coa`), reply in-channel:
   *“✅ Profile saved. Now drop your COA file (.xlsx/.csv) in this channel — or tap **Use standard SG SME COA**.”*
4. **COA ingest:** the bot detects an uploaded spreadsheet in the channel (or the standard-COA button),
   parses it (COA sheet: `Account code | Description | Account type | Financial Statement | Nature | AI Search Keywords`),
   stores it under the client, seeds `category_mapping` from the standard universal categories, sets status `active`.
5. **Edit later:** `/ledgr settings` re-opens the same modal pre-filled.

The COA parser reuses the already-built loader logic **minus Sys_Config** (Sys_Config is removed; profile
metadata now comes from the modal, not the sheet).

## 3. Financial-year model (FYE-month → FY)
- FYE = the **last day of `fye_month`** (e.g. `fye_month = 3` → 31 March each year).
- A document dated `d` belongs to the FY that **ends on the first FYE on/after `d`**; the **FY label is the
  calendar year of that FYE**.

```
def fy_for_date(d: date, fye_month: int) -> int:
    fye_this_year = last_day_of_month(d.year, fye_month)   # date(d.year, fye_month, <last day>)
    return d.year if d <= fye_this_year else d.year + 1
```
Examples (fye_month = 3 / March): `2025-03-15 → FY2025`; `2025-04-02 → FY2026`.
Calendar-year client (fye_month = 12): `2025-06-01 → FY2025`; `2026-01-01 → FY2026`.
Late-arriving prior-year documents route to their correct FY automatically (e.g. a Dec-2024
statement sitting in FY2025).

## 4. Routing — archive + workbook
For each processed document, using the FY from §3 on the document's extracted date:
- **Archive (GCS):** `gs://{bucket}/{client_id}/FY{year}/{purchase|sales|bank}/{original_filename}`.
- **Workbook:** append the extracted rows to that FY's consolidated workbook for the client's software:
  - `Ledger_FY{year}.xlsx` — `Purchase` + `Sales` sheets (no Sys_Config sheet, per the locked output decision).
  - `BankStatement_FY{year}.xlsx` — one sheet per bank account (existing `BankStatementExporter`).
- Doc-type → destination: purchase/receipt → Purchase sheet + `purchase/`; sales → Sales sheet + `sales/`;
  bank_statement → bank workbook + `bank/`.

## 5. Impact on existing code
- **`export/client_context.py`** (built this session): **remove the `Sys_Config` reading** for profile
  metadata. `ClientContext` profile fields (region, software, currency, gst_registered, fye_month) come from
  the Firestore profile, not a sheet. Keep the COA / Category_Mapping / Entity_Memory parsing of uploaded
  client data. `FirestoreClientStore` becomes the real backend (profile + subcollections).
- **`export/categorizer.py`** (built this session): unchanged — already reads coa/category_mapping/entity_memory
  from `tool_context.state`; the `before_agent_callback` loads them from Firestore keyed by channel→client.
- A new **`fy.py`** (or in client_context) holds `fy_for_date`.
- Onboarding (modal handlers) + COA ingest live in the Slack app layer (`app/`), built with the Slack glue (task #4).

## Out of scope (later)
- Malaysia region field + SST; multi-currency profile.
- OAuth install / per-workspace bot tokens (task #5).
- Corrections → Entity_Memory learning flow (task #6/#7) — the store shape is defined here; the ✏️ flow is later.
- Category_Mapping bootstrap (propose category→account when empty) — noted in build-map step 6.

## Verification
- Firestore round-trip: write a profile + COA, read it back into `ClientContext`; assert fields.
- `fy_for_date` unit table (FYE Mar / Dec; boundary dates) — deterministic.
- Onboarding modal submit → profile doc created + channel index + status transitions (pending_coa → active).
- End-to-end: a document with a known date routes to the correct `FY{year}` GCS path + workbook.
