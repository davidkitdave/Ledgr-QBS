# Ledgr-QBS — Live Smoke QA Checklist (2026-06-14)

Verifies Plan A (template/onboarding/HITL) + Plan B (extraction accuracy) against a **live** Slack
workspace (`qbs-ai.slack.com`, socket-mode bot) using Cast Unity test firms in `~/Desktop/LocalTest`.

Status legend: [ ] pending · [~] in progress · [x] pass · [!] FAIL (file follow-up)

## 0. Pre-flight
- [x] Test suite green — 830 passed
- [x] Slack surface chosen (desktop computer-use) + screen access granted
- [x] **CRITICAL: stale bot found** — PID 72500 started Jun 13 23:19, predated ALL Plan A/B
  commits (Jun 14 02:13–15:10). Killed it; restarted fresh bot (PID 58150) from main HEAD
  3315501. Everything previously seen in Slack was old code. QA below = fresh bot.

## 1. Client onboarding (Plan A T3–T4, T2)
- [x] `/ledgr profile` recognized (FAILED on stale bot → "Ledgr slash commands"; PASS on fresh bot)
- [x] Profile card shows software=Xero, FYE=October, GST=Not registered (Auditair) — echo works

## 1. Client onboarding (Plan A T3–T4, T2)
- [ ] `/ledgr settings` on a fresh channel → onboarding modal opens
- [ ] Submit profile (e.g. Xero, FYE month, region, GST-registered) → **profile-summary card** posts
- [ ] COA: upload COA xlsx **or** tap "Use standard SG SME COA" → client shows active
- [ ] `/ledgr profile` → echoes saved profile incl. accounting software
- [ ] Delivery summary later names the chosen software ("…to your Xero FY… ledger")

## 2. Single-invoice extraction accuracy (Plan B)
- [ ] Clean tax invoice → correct direction (purchase vs sales), vendor, GST = SR (no flag)
- [ ] Invoice number + date populated (Xero `*InvoiceNumber`/`*InvoiceDate`/`*DueDate` non-blank)
- [ ] Discount + tax invoice → reconciles (Σ lines == subtotal)
- [ ] Dividend doc → NOT booked as a purchase with client as vendor
- [ ] Auditair docs → reconcile (was 0%)

## 3. Multi-currency / FX (Plan B T3/T3b)
- [ ] IDR/USD multi-receipt bundle → split into separate lines, each correct
- [ ] Foreign-currency doc vs SGD client → `needs_fx_review` surfaced (not silently wrong)

## 4. Multi-document batch (Plan A T9 + Plan B T3)
- [ ] Drop several docs at once → **one** "Batch complete" job summary (no per-doc spam)
- [ ] Multi-invoice / multi-receipt PDF → split per invoice; SOA cover page skipped

## 5. HITL review + edit loop (Plan A T5–T7)
- [ ] Review card names the **uploaded filename** (not just extracted identity)
- [ ] "Edit" button opens Block-Kit modal pre-filled with proposed lines
- [ ] Change an account code + tax code, submit → ledger reflects the edit (not the original)
- [ ] Reject → nothing appended to ledger

## 6. Learning / "getting smarter" (Plan A T8, ADR-0004)
- [ ] An edit persists as a per-client Correction (vendor → account/tax)
- [ ] Re-drop a similar doc from the **same vendor** → bot auto-applies the corrected mapping

## 7. Conversational Q&A in-channel
- [ ] @mention the bot with a question about a processed doc → relevant answer (Q&A lane)
- [ ] Follow-up correction in same thread → bot adjusts

## 8. Bank-ledger continuity (Plan B T7, memory: continuous+sorted)
- [ ] Drop another Akar month → running balance continues; B/F + cross-month check holds; gaps flagged

## 9. Robustness
- [ ] Unreadable / empty / unsupported file → "❌ Couldn't read this file" (NOT "Processed")

## Live results (fresh bot, Auditair channel — Xero / FYE Oct / non-GST)

### PASS
- `/ledgr profile` returns profile card w/ software=Xero, FYE, GST status (Plan A T2/T4)
- HITL review card renders w/ Approve/Edit/Reject; holds doc (0 posted) until decision (Plan A T5–7)
- Edit modal: per-line Account/Tax/Amount, pre-filled from proposal, COA dropdown = client COA
- Edit honored: "Approved with edits" → posted (Plan A T6/T7)
- **Honours accounting software**: posted to **Xero** ledger, not QBS (Plan A T1) ✅ key fix
- **FY routing**: Dec-2025 doc → **FY2026** under Oct FYE ✅
- **FX guard**: USD doc vs SGD base → "needs fx review, no exchange rate" — refused silent convert (Plan B T3b)
- Safe default: direction uncertain → defaulted to purchase + held for review (no bad auto-post)
- Workbook delivered (Ledger_FY2026.xlsx) after post
- No errors/tracebacks in bot log across multiple docs

### FAIL / PARTIAL
- [!] **Learning loop did NOT apply** (Plan A T8 / ADR-0004): edited D37 Podaima → 5-1000 Cost of
  Sales, posted; then dropped D36 (same vendor Darrell Podaima) → bot STILL proposed 6-3000
  Professional Fees. Correction not auto-applied. → background investigation dispatched.
- [~] **Filename not captured**: status + review card show generic `document.pdf` instead of the
  uploaded filename (Plan A T5 intent). Line is named by content (25-D37-SFS) but doc label is generic.
- [~] **Direction low-confidence**: clear "To: Auditair" bill-to invoice still flagged "direction
  unknown" (Plan B T2 gap on this doc shape).
- [~] **FX resolution path in HITL**: Edit modal has no FX-rate field; posting "Approved with edits"
  pushed the doc through despite unresolved USD→SGD (need to verify what amount/currency landed).

## Additional live results

### PASS (batch)
- 4-file drop → ONE job summary updating in place (Reconciling→Extracting→Categorising→
  "Processed 4 documents"), NO per-doc spam (Plan A T9) ✅
- Multi-invoice bundle split: combined "EXP25-D03 transfer" doc → invoices #2/#3 with mixed
  currencies THB + USD detected separately (Plan B T3) ✅
- Tax classifier with reasoning: AAA-25-011 line flagged "NT: supplier not GST-registered /
  no GST line" — correct for non-GST client (Plan B T6) ✅

### CONFIRMED BUGS (root-caused)
1. [!] **Learning loop broken — corrections never persist (HEADLINE)**
   - `_persist_corrections` (accounting_agents/slack_runner.py:721) reads `first.get("vendor_name")
     or first.get("issuer_name")`, but serialized NormalizedInvoice has no such keys — vendor is
     nested at `supplier.name`/`customer.name`. `vendor` is always None → early return → no
     add_correction. Verified: Firestore entity_memory for client-97b148846c8f is EMPTY.
   - Categorizer + reload path are CORRECT; only persist side broken.
   - Same wrong-key also hits `_doc_label_from_state` (slack_runner.py:695) → cards never show vendor.
   - Test masks it: tests/test_slack_runner.py:709-718 uses a fake flat `vendor_name` key that
     `_inv_to_dict` never produces.
   - FIX: read nested party by direction:
     `party = first.get("supplier") if first.get("doc_type","purchase")=="purchase" else first.get("customer"); vendor=(party or {}).get("name")`
     + fix the test fixture to the real serialized shape.
2. [!] **Conversational Q&A non-functional**: @Ledgr question → replies in thread but only returns
   a canned capabilities menu ("summarize spend / P&L / GST threshold"); asking those exact options
   loops the same menu. Never produces grounded data answers. Needs investigation (tools wired? errors?).

### MINOR / UX
- Doc label `document.pdf` instead of uploaded filename on status + review cards (Plan A T5 intent)
- Direction "unknown" on every Auditair purchase invoice despite clear "To: Auditair" (Plan B T2)
- Status wording self-contradicts: "not reconciled (reconciled; …)"

## Not yet tested live (deferred)
- Akar bank-statement continuity on fresh bot (drop jun-2025) — Plan B T7
- Robustness: unreadable/empty/unsupported upload rejection — §9
- Verify actual numbers/currency landed in the Xero workbook (esp. unconverted USD after edit-approve)

## Fix applied (this session)
- **FIXED the learning vendor-key bug**: `_persist_corrections` now reads the nested counterparty
  (`supplier.name`/`customer.name`) via new `_vendor_from_inv_dict` helper, instead of the
  non-existent flat `vendor_name`/`issuer_name`. Corrected the masking test fixture + added a
  real-serialized-shape test (purchase + sales). Suite: **831 passed**, no new lint.
- **PROVEN live**: after restart + an edit, Firestore `clients/client-97b148846c8f/entity_memory`
  went 0 → 1 doc `{name: "Darrell Podaima", mapping_code: …}`. Corrections now persist. ✅

## ✅ LEARNING LOOP VERIFIED END-TO-END (post-fix)
- Taught vendor "Darrell Podaima" → 5-1000 Cost of Sales (distinctive, ≠ LLM default 6-3000).
- Firestore entity_memory updated to `{name: "Darrell Podaima", mapping_code: "5-1000"}`.
- Dropped a BRAND-NEW Podaima invoice (25-D31, never edited) → Edit modal auto-filled
  **5-1000 — Cost of Sales** on both lines (not 6-3000). The bot applied the learned correction.
- "Getting smarter" now works. (Was fully broken: stale bot + vendor-key bug.)

## Fixes committed on branch `fix/ledgr-hitl-learning-and-qa` (835 tests pass, lint clean)
1. `0060854` fix(hitl): persist corrections under nested vendor — **verified live** (auto-applied 5-1000).
2. `e8b21df` fix(hitl): only learn lines the human changed (multi-line collision) — unit-tested
   (collision regression + unchanged-line skip); **live re-verify pending** (Mac locked).
3. `2c787d5` fix(qa): feed the real question to qa_agent via state + instruction provider
   (was returning a canned menu) — unit-tested; **live re-verify pending**.

## Live re-verification still pending (Mac screen locked mid-session)
- Q&A: ask a real question → expect a grounded, tool-backed answer (not the menu)
- Multi-line collision: edit ONE line of a 2-line invoice → expect only that line learned
- Akar bank continuity: drop jun-2025 → running-balance continuity
- Unreadable-file rejection: `/tmp/QA Unreadable Test.exe` staged → expect "❌ Couldn't read this file"

## ✅ Fixes 4–5 DONE + LIVE-VERIFIED (this session)
- **Filename/validation** (`a5cf502`): `_resolve_file_name` threads the real Slack filename into
  both file handlers. Live: re-dropped the `.exe` → bot replied "❌ Couldn't read this file …
  got `.exe` … supported: .pdf/.png/…" (rejected BEFORE Gemini; real name shown). Was: silent
  "Processed" + 400 error + `document.pdf` label.
- **Bank recompute** (`d56259c`): `_is_formula_or_missing` now treats non-numeric balance cells
  (currency strings) as untrusted. Live: re-dropped Akar jun-2025 → "Added Jun 2025 (39
  transactions) to your QBS Ledger FY2025 ledger", no crash. Was: ValueError on `float('SGD')`.
- **Pre-fill amount+tax (#6/feature) — DEFERRED:** investigation showed the whole edit pipeline
  (modal → _edits_from_view_state → apply_decision → _dict_to_inv) is keyed on `tax_code`/`amount`,
  but the model uses `tax_treatment`/`net_amount` (only `account_code` aligns). So "pre-fill" is
  really "make tax/amount editing work end-to-end" — a HITL-path refactor with test churn.
  Recommended approach: map edit keys→model fields in apply_decision + read net_amount/tax_treatment
  in the modal pre-fill (blocks.py). NOT done now (rushing it at session-end risks the core HITL path).

## §8 Bank continuity FAIL (found 2026-06-14 — FIXED, see above)
- [!] Dropping a new month (Akar jun-2025) onto an existing FY bank ledger CRASHES:
  `ledger_store.py:296 _recompute_balances → running = float(bal) → ValueError: could not
  convert string to float: 'SGD'`. Month never posts (stuck "Finalising… 0 posted").
- ROOT CAUSE: `_recompute_balances` assumes every balance cell is numeric, but the sheet has
  non-numeric balance cells (currency string 'SGD' / "BALANCE B/F" carry-forward rows).
- FIX: guard non-numeric balance cells in `_recompute_balances` / `_read_bank_blocks` (skip or
  treat as section markers) so recompute is robust to header/B-F/currency rows.

## §9 Robustness FAIL + root cause (found 2026-06-14, NOT yet fixed)
- [!] **Unsupported file (.exe) was NOT rejected** — it reached Gemini and errored
  `400 INVALID_ARGUMENT "The document has no pages"`, then showed a misleading "Processed 1 document".
- ROOT CAUSE (single): `_file_shared` (slack_runner.py:1127) calls `process_file_event` WITHOUT
  `source_filename`, so it defaults to `"document.pdf"`. Therefore:
  1. `_validate_download` always checks `.pdf` (supported) → can never reject unsupported types.
  2. Review cards/status always show `document.pdf` instead of the uploaded filename (== Plan A T5 gap).
- FIX: thread the real Slack filename (+ filetype) from the file event into `process_file_event`
  (fetch via files.info for file_shared; use event['files'][0]['name'] on the message path).
  Fixes rejection AND the card label together. (NOTE: Slack also re-typed the .exe to a text
  snippet — but that's moot once the real name/extension is passed.)

## Secondary bug found during re-verify (NOW FIXED — see commit e8b21df above)
- [!] **Multi-line invoice learns the WRONG account.** The Edit modal submits ALL lines (changed
  or not); `_persist_corrections` writes one Correction per line, all keyed by the same vendor →
  **last line wins**. Editing Line 1→5-1000 while Line 2 stayed 6-3000 persisted 6-3000.
  - Recommend: only persist lines whose value DIFFERS from the proposal in state (needs the
    existing 2-3 write-every-line tests updated). Alt: key by vendor+line, or first-changed-line.

