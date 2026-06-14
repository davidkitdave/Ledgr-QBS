# Ledgr-QBS — Plan for the next QA / build session

Carries over from the 2026-06-14 live QA. Branch: **`fix/ledgr-hitl-learning-and-qa`**
(11 commits, 840 tests pass, not merged). Companion doc: `2026-06-14-live-smoke-qa-checklist.md`.

**Start every session by restarting the bot from HEAD** (a stale long-running bot runs old code):
```
pkill -f slack_runner; sleep 2
nohup .venv/bin/python -u -c "from dotenv import load_dotenv; load_dotenv('.env'); import os,logging; os.environ.setdefault('GOOGLE_GENAI_USE_VERTEXAI','FALSE'); logging.basicConfig(level=logging.INFO,force=True); from accounting_agents.slack_runner import main; main()" > /tmp/ledgr_bot.log 2>&1 &
```
Workspace `qbs-ai.slack.com`; channels: #akar-enterprises-pte-ltd (QBS, FYE Dec, bank),
#auditair-international-pte-ltd (Xero, FYE Oct, non-GST, invoices). Test docs in
`~/Desktop/LocalTest/TestDoc/Cast Unity`. Reference (correct) bank formula:
`~/Downloads/Rosebery Partner Pte. Ltd. - BankStatement_FY2024.xlsx`.

---

## DONE this session (committed on branch, unit-tested; ✅ = also live-verified)
1. ✅ Learning vendor-key — `_persist_corrections` reads nested `supplier/customer.name`.
2. ✅ Multi-line collision + call-site — only persist genuinely-changed lines; diff vs PRE-resume state.
3. ✅ Q&A routing — question carried in state via `qa_instruction` provider (no more canned menu).
4. ✅ Filename/validation — `_resolve_file_name` threads real Slack name (rejects `.exe`, labels cards).
5. ✅ Bank recompute crash — `_is_formula_or_missing` treats non-numeric balance as untrusted.
6. ⏳ Bank Math_Check `#VALUE!` — formula now `N()`-coerced + `❌ Exp:` detail. **Committed, NOT yet
   live-verified** (commit `209d543`; bot was last restarted before this commit).

---

## NEXT SESSION — prioritized

### A. Quick wins (do first)

**A1. Live-verify the bank fixes** (no code; restart bot first)
- Re-drop an Akar month (e.g. jul-2025) → confirm the rebuilt `BankStatement_FY2025.xlsx`:
  - Math_Check column shows ✅ / GAP / `❌ Exp: <n>` — **never `#VALUE!`**.
  - Running balance chains continuously across months.
- Open the workbook and check whether any rows have a **genuinely missing extracted balance**
  (vs. the old pre-fix May rows). If balances are missing at extraction time, that's a separate
  **extractor** check: `invoice_processing/extract/bank_statement_extractor.py` (does it reliably
  capture per-row balance? digital vs scanned path). The May rows in the current Akar sheet are
  stale (old code) — a full re-process or fresh channel gives a clean read.

**A2. Rejection-tally UX** (small)
- Problem: an unsupported upload now correctly posts "❌ Couldn't read this file", but the batch
  job summary still counts it as **"Processed 1 document — 0 posted"** (misleading).
- Where: the message/file_share handler aggregates per-doc results and builds the summary via
  `job_summary_text(...)` — `accounting_agents/slack_runner.py` ~line 1405–1430 (the loop that
  collects `result["status"]`). `process_file_event` returns `status="rejected_unreadable"`.
- Fix: tally `rejected` separately and render e.g. "Received N files · X processed · Y rejected ·
  Z posted". Add a test alongside the existing batch-summary tests in `tests/test_slack_runner.py`.

### B. Medium builds

**B3. Duplicate-detection UX**
- Today: dedup is silent — `_seen.seen_before("file:<id>")` (per-file within session) +
  `seen_doc_keys` on the FY pointer (content-based, on append, `ledger_store.py`). It won't
  double-post, but the user gets no signal.
- Want: when a doc was already processed, tell the user **when + which ledger/FY**, and ask them to
  **confirm new-vs-duplicate** before skipping (a Block-Kit "Add anyway / Skip" prompt).
- Approach: surface the matched `seen_doc_keys` entry (store its date/FY/file label when first
  appended, in the pointer doc), and add an approval-style confirm in the file path. Keep the
  internal idempotency as the safety net.

**B4. Q&A FY-resolution** (so Q&A actually answers, not "ledger not loaded")
- Today: `answer_question` (`slack_runner.py` ~896–978) reads `fy` from a FRESH per-question
  session → always `"unknown"` → `read_rows` finds no workbook → "not loaded". (Routing is fixed;
  data lookup is not.)
- Fix: resolve `client_id` + the relevant FY from the client profile / ledger pointers (enumerate
  `clients/{id}/ledgers/*` for the latest FY, or parse an FY from the question), then `read_rows`
  for that FY. Seed the profile into the Q&A session like the document path does
  (`_profile_state_delta`).

### C. Larger / design-first

**C5. Channel folder structure (Sales / Purchase / Bank-Statement + per-FY)** — RESEARCH FIRST
- Old model (Rosebery `Sys_Config`): Google-Drive folder IDs SALES/PURCHASE/BANK/BANK_ARCHIVE.
- Want Slack-native: per-channel folders, auto-filed by doc type + FY, created on channel setup,
  and outputs routed into the right folder/FY even when dropped in the message.
- UNKNOWN: what Slack actually supports — the UI shows a "Folder" item in the "+" menu and an "FS"
  tab on channels. **Research deliverable:** confirm Slack's folder/canvas/list API + bot scopes,
  then a short design doc BEFORE any code.

**C6. Edit-pipeline field alignment (= "pre-fill amount + tax")**
- The whole edit path is keyed on `tax_code`/`amount`, but the model uses `tax_treatment`/
  `net_amount` (only `account_code` aligns). So editing tax/amount silently fails + the modal can't
  pre-fill them (blank). `_dict_to_inv` does `InvoiceLine(**ld)` → would also break on bad keys.
- Files: `app/blocks.py:invoice_edit_modal` (~575, pre-fill reads), `accounting_agents/nodes.py`
  `EDITABLE_LINE_FIELDS` (:148) + `apply_decision_node` (:377), `_edits_from_view_state`
  (`slack_runner.py` ~756), `_persist_corrections`.
- Fix (contained): map edit keys → model fields in `apply_decision_node`
  (`tax_code→tax_treatment`, `amount→net_amount`) and read `net_amount`/`tax_treatment` in the modal
  pre-fill. Update the fixtures that encode the fictional `tax_code`/`amount` line shape.

**C7. Confirm = confidence** — an un-edited Approve should reinforce the vendor→account mapping
(today only an *edit* teaches). Guard against over-trusting on a single confirm.

**C8. Doc image in Edit modal** — show a page image of the document (Block-Kit image block) above
the form. Approximation of side-by-side within Slack's modal limits (true side-by-side needs the
hosted web page in C5's spirit).

**C9. Chat-amend a posted ledger row** — write-capable: message the agent to fix an already-posted
entry; locate the row (vendor/date/amount), edit the workbook, re-upload, update the learned
correction; require a confirm step (it changes the book of record).

### D. Minor polish
- "direction unknown" on every clearly-addressed bill-to invoice (Plan B T2 confidence gap).
- "not reconciled (reconciled; …)" self-contradicting status wording.
- Multi-doc UX at 10 docs: verified at 4 (one job summary); confirm at 10.

---

## Suggested order for the next session
A1 (verify bank) → A2 (rejection tally) → B4 (Q&A FY) → B3 (dedup UX) → C6 (edit fields) →
C5 research → then C7/C8/C9. Commit each via TDD; restart bot + live-verify after each batch.
