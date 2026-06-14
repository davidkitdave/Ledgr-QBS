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

**B3. Duplicate-detection UX  (CONFIRMED live 2026-06-14: re-dropping jun-2025 → "Processed 1
document — 0 posted", no explanation)**
- Today: dedup is silent. Bank path: `ledger_store.append_rows` reads `seen_doc_keys` from the
  Firestore pointer; if `doc_key in seen_doc_keys` → `deduped+=1, appended=0` (lines ~444–483).
  Invoice/file path also has `_seen.seen_before("file:<id>")` within a session. It won't
  double-post, but the only signal is the misleading "0 posted".
- ROOT INSIGHT: **dedup state lives in Firestore (`seen_doc_keys`), decoupled from the Excel.**
  So a user who deletes/edits rows in the workbook and re-drops the file gets silently skipped —
  there is **no way to force a re-process/replace** today. (This is what David hit live.)
- Want:
  1. **Explain, don't emit "0 posted"** — agent says e.g. "I already recorded this June statement on
     <date> in your FY2025 ledger (39 transactions), so there's nothing new to add."
  2. **Offer a re-process / replace path** — Block-Kit "Re-process (replace) / Skip". Re-process must
     clear that `doc_key` from `seen_doc_keys` (and/or rebuild the month's block) so the corrected
     data posts. Needed especially after a fix (e.g. the new N() bank formula) or a manual edit.
  3. Confirm new-vs-duplicate when ambiguous.
- Approach: store date/FY/file label alongside each `seen_doc_keys` entry (in the pointer) so the
  agent can cite when+where; add the confirm/replace action; keep internal idempotency as the net.

**B3b. Agentic status/result messaging (NEW — cross-cutting UX theme David raised)**
- Today: results are RIGID TEMPLATES — "📥 Processed 1 document — 0 posted to your ledger",
  "✅ Processed", "❌". They don't explain *what happened or why* (deduped? rejected? partially
  posted? needs review?). David: "it needs to be the agent really replying about what he's doing …
  rather than a random rigid formula."
- Want: replace the templated status/summary strings with **agent-authored, situation-aware
  narration** — for dedup, rejection, partial post, FX-hold, needs-review, and normal success.
  Keep it concise + accurate (grounded in the real `result` dict: status/appended/deduped/posted).
- Where: the status/summary builders in `accounting_agents/slack_runner.py` (`_post_status`,
  `_update_status`, `job_summary_text`, the per-doc + batch result rendering ~1256–1430) and
  `deliver_node` summary text (`nodes.py`). Decide: LLM-generated vs richer rule-based templates
  (LLM gives natural narration but adds latency/variability — likely a small templated-with-reasons
  layer is enough, with the reason taken from the result status). Tie into A2 (rejection tally) and
  B3 (dedup explanation) — they're the same "explain the outcome" gap.

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
A1 (verify bank) → A2 (rejection tally) + B3b (agentic messaging — same "explain the outcome" gap) →
B3 (dedup UX + re-process/replace) → B4 (Q&A FY) → C6 (edit fields) → C5 research → then C7/C8/C9.
Commit each via TDD; restart bot + live-verify after each batch.

NOTE: the bot currently running (PID 79489) predates BOTH the bank N()-formula fix (`209d543`) and
this plan — so to even see the corrected bank formula / behaviour, restart from HEAD first. To let
David re-process the jun-2025 statement he edited, the jun-2025 `doc_key` must be cleared from the
Akar FY2025 pointer's `seen_doc_keys` (B3's replace path) — otherwise it stays deduped.
