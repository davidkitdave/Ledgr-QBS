# Light-path live smoke checklist

Manual QA for the live **`ledgr_slack` + `ledgr_agent`** stack (ADR-0032, ADR-0036).
Do **not** use archived checklists under [`archive/`](archive/) — they target the removed
`accounting_agents` / HITL / COA-upload flow.

Status legend: `[ ]` pending · `[~]` in progress · `[x]` pass · `[!]` fail

See also: [testing-process.md](testing-process.md) · [erp-import-matrix.md](erp-import-matrix.md)

---

## 0. Pre-flight

- [ ] `uv run pytest` green (~929 tests)
- [ ] Kill stale Socket Mode bots before testing ([AGENTS.md](../../AGENTS.md))
- [ ] `LEDGR_FIRESTORE_NAMESPACE` set for dev (not prod)
- [ ] Fresh bot: `uv run python -m ledgr_slack` prints "Bolt app is running"
- [ ] Optional live eval: `scripts/ledgr_eval_light.sh` (needs `GOOGLE_API_KEY`)

---

## 1. Firm install

### Socket Mode (dev)

- [ ] `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN` in `.env`
- [ ] Bot scopes match [`slack/manifest.json`](../../slack/manifest.json)
- [ ] `/invite @Ledgr` → welcome card + **Set up this client** button

### OAuth (production)

- [ ] `GET /slack/install` → Allow → token saved (no 500)
- [ ] Cloud Run SA has Firestore + GCS IAM ([slack-setup.md](../slack-setup.md) step 4b)

---

## 2. Client onboarding (per channel)

One Slack channel = one client. No COA spreadsheet upload on the live path.

- [ ] Bot joins channel → welcome card (bot user only)
- [ ] **Set up** button or `/ledgr settings` → 5-field modal (name, region, FYE month, software, GST)
- [ ] Channel name prefill (e.g. `acme-pte-ltd` → "Acme Pte Ltd")
- [ ] Submit → profile summary card; `status: active` immediately
- [ ] `/ledgr profile` echoes saved profile
- [ ] Re-open `/ledgr settings` → same `client_id`, edits preserved
- [ ] Drop file **before** setup → soft gate ("run `/ledgr settings` first")
- [ ] Confirm: **no** COA upload step or `pending_coa` status

---

## 3. Document intake (Slack UX)

- [ ] Single PDF invoice → 👀 reaction, status, delivery card, `.xlsx` upload
- [ ] Receipt image (png/jpg) accepted
- [ ] Unsupported file (.docx) → clear error (not "Processed")
- [ ] Empty/corrupt PDF → error card, no ledger append
- [ ] Multi-file drop → **one** job summary (not per-doc spam)
- [ ] Filename on status/delivery card shows real upload name
- [ ] Plain text / @mention (no file) → document-only reply (no chat Q&A)

---

## 4. Processing — commercial documents

Live path: `read_doc` (one Gemini call) → `build_sheets` (deterministic ERP rows) → deliver.

- [ ] Purchase invoice: correct vendor, amounts, tax from **printed** labels
- [ ] Sales invoice → **Sales** sheet
- [ ] Receipt processed as commercial document
- [ ] Credit note → negative amounts in export
- [ ] Multi-invoice PDF → split into separate rows/docs
- [ ] SOA-only PDF → not posted (or only when sole content)
- [ ] Telco / summary-grain bill → SR/ZR buckets, not hundreds of appendix lines
- [ ] **Known gap:** account codes blank on light path (ADR-0036)

---

## 5. Processing — bank statements

- [ ] Bank PDF → `BankStatement_FY{fy}.xlsx` in channel
- [ ] Running balance reconciles on statement
- [ ] Multi-account / multi-currency → separate tabs (if applicable)
- [ ] Second month drop → balance continues from prior (cross-month)

---

## 6. Credits and billing

- [ ] Insufficient credits → blocked **before** Gemini call
- [ ] Delivery card shows coin balance footer
- [ ] Credit charge matches document count/pages

---

## 7. Financial year (FY)

Client `fye_month` drives routing. Examples (verify at least one):

| FYE month | Doc date | Expected FY |
|-----------|----------|-------------|
| December (12) | 2026-01-10 | FY2026 |
| March (3) | 2025-03-15 | FY2025 |
| March (3) | 2025-04-01 | FY2026 |
| October (10) | 2025-12-15 | FY2026 |

- [ ] Delivery summary names correct FY
- [ ] Batch spanning two FYs → two workbooks or correct split

---

## 8. ERP output (per accounting software)

Run **one channel per ERP** (or edit profile between drops). See [erp-import-matrix.md](erp-import-matrix.md) for import tick-boxes.

| ERP | Purchase columns spot-check | Sales sheet | Import into ERP |
|-----|----------------------------|-------------|-----------------|
| QBS Ledger | Date, Invoice No, Vendor, Sub Total, Tax, Total | Yes | [ ] |
| Xero | `*ContactName`, `*InvoiceNumber`, `*TaxType`, amounts | Yes | [ ] |
| AutoCount | DocNo, TaxCode, TaxableAmt, Tax | Yes | [ ] |
| SQL Account | `_DOCNO`, `_DATE`, `_TAXCODE`, `_TAXAMT` | Yes | [ ] |

- [ ] First drop creates `{Client} - Ledger_FY{fy}.xlsx`
- [ ] Second drop appends rows (does not replace whole file)
- [ ] Re-drop same invoice → dedup/replace UX behaves sensibly
- [ ] Only client's ERP columns emitted (not all four systems)

---

## 9. Regions and edge cases

- [ ] Singapore GST invoice (SR / ZR from printed labels)
- [ ] Malaysia SST invoice (if available)
- [ ] Non-GST client → sensible tax handling
- [ ] Foreign currency doc → no silent wrong conversion (note behavior)

---

## 10. Minimum smoke session (quick path)

If time is short, run these nine steps in a **fresh** Socket Mode session:

1. New channel → invite bot → complete modal for **each ERP** (4 channels or 4 profile edits)
2. One **purchase invoice** per ERP → open xlsx → verify columns + FY
3. One **sales invoice** → lands on Sales sheet
4. One **receipt** + one **credit note**
5. One **bank statement** → correct FY bank workbook
6. **Batch** of 3 files → single job summary
7. **Re-drop** same invoice → dedup/replace UX
8. Channel with **no profile** → soft gate message
9. **Out-of-credits** firm → blocked before processing

---

## Do NOT test (removed features)

| Old feature | Status |
|-------------|--------|
| HITL Approve / Edit / Reject | Removed — auto-post |
| COA spreadsheet upload | Removed (ADR-0036) |
| Entity memory / learning from edits | Not wired on light path |
| In-channel chat Q&A | Archived — document-only reply |
| `accounting_agents` graph | In `legacy/` only |
