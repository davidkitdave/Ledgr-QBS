# Ledgr-QBS — UX findings: agentic voice, bank-vs-ledger, Q&A tools, naming, dup-data

Branch **`fix/ledgr-hitl-learning-and-qa`**. Captured during the 2026-06-14 live QA from David's
feedback after testing real Cast Unity docs in #akar-enterprises-pte-ltd (QBS, FYE Dec, bank).
ADK-grounded via the `adk-docs` MCP (Events doc) — see notes per item.

These are the things that made the bot feel "robotic" or wrong, plus two confirmed data/Q&A bugs.

---

## Confirmed root causes (evidence-backed)

### F1. Q&A answers are weak because the agent has the WRONG tools (not a format, not FY)
- B4 (FY resolution) **works**: `read_rows(client_id=CL-7da82c43, fy=2025)` returns **159 rows**.
  So "ledger not loaded" is no longer a data-loading problem.
- BUT `accounting_agents/qa_agent.py` exposes only **three invoice-oriented tools**:
  `summarize_by_category` (sums `"Source Amount"` per `"Account Code / COA"`), `pnl_for_fy`,
  `gst_threshold_check`. **None understand bank columns** (`Date / Description / Withdrawal /
  Deposit / Balance`).
- So "total withdrawal for October 2025" has **no matching tool**. The `qa_agent` LlmAgent
  (`MODEL_LITE`, single-turn) is genuinely replying (it is NOT a canned string), but with no bank
  tool it punts ("I can only access ledger data… upload the FY ledger" / "specify the FY").
- **Answer to David's question:** it's the agent replying, and yes — it lacked the right tools.
- **Fix:** add bank-aware Q&A tools (e.g. `bank_totals` → withdrawals/deposits/closing balance,
  optionally month-filtered) and teach the `qa_agent` instruction to detect bank vs invoice data and
  pick the right tool. Keep tools pure (operate on `state["ledger_data"]`).

### F2. September is physically duplicated in the bank workbook (data bug)
- Akar FY2025 bank sheet: **9 `BALANCE B/F` rows** + per-day counts that are multiples
  (`18/09: 12`, `20/09: 12`, `12/09: 8`, `30/09: 8`). Clear sign the same statements were appended
  multiple times during the **doc_key format transition** (old `F<fileid>:OCBC:acct` keys vs new
  content key `OCBC - 0001:acct:period`).
- `SlackLedgerStore._merge_bank_statement` has **no row-level dedup** — it re-merges ALL existing
  rows on every append. Firestore `seen_doc_keys` only blocks *new* uploads; already-duplicated rows
  persist and re-sort each time.
- **Fix:** (a) row/transaction-level dedup inside `_merge_bank_statement` (key on
  date+description+amount+balance) as a safety net; (b) one-time clean of the Akar workbook;
  (c) collapse the 9 `BALANCE B/F` rows into one continuous running balance per
  `bank-ledger-continuous-sorted` memory.

### F3. Bank statement is mislabelled "ledger" (wording bug)
- `accounting_agents/nodes.py` `deliver_node` (~L772): bank summary is
  `f"📒 Added … to your {target}FY{fy} ledger."` with `target="QBS Ledger "` →
  **"Added Oct 2025 … to your QBS Ledger FY2025 ledger."** A bank statement is not a ledger.
- **Fix:** branch wording by `kind`: bank → "added to your **<Client> – Bank Statement FY2025**";
  invoice → "added to your **<Client> – Ledger FY2025**". Don't say "ledger" for bank docs.

### F4. File naming should be client-scoped
- `ledger_store.py` L480/483: `BankStatement_FY{fy}.xlsx` / `Ledger_FY{fy}.xlsx`.
- **Requested:** `<Client Name> - BankStatement FYXXXX.xlsx` /
  `<Client Name> - Ledger FYXXXX.xlsx` (matches the reference
  `Rosebery Partner Pte. Ltd. - BankStatement_FY2024.xlsx`). Thread `client_name` from the profile
  into the filename. Confirm download/relabel on every append.

### F5. Status messages are fixed/robotic — don't say WHAT the doc is
- `slack_runner.py` `_STAGE_LABELS` are static: `🔍 Classifying…`, `📊 Extracting (bank statement)…`,
  `📦 Finalising…`. The user never learns the doc was identified as e.g. "OCBC bank statement,
  Oct 2025".
- **ADK grounding (Events doc):** the run stream carries `event.partial` streaming text chunks and,
  with Gemini thinking enabled (`google.genai.types.ThinkingConfig(include_thoughts=True)` via a
  planner / `generate_content_config`), `part.thought == True` parts that are distinct from the final
  answer. Detect them in the event loop and surface separately from `is_final_response()` output.
- **Two-tier fix:**
  - *Pragmatic (low risk):* after `classify_node`, post a specific line built from state
    (`doc_type`, `bank_name`/vendor, period) — "🔍 Looks like an **OCBC bank statement** — reading
    **Oct 2025**…". Replaces opaque "Classifying…".
  - *Aspirational (design-heavy):* stream real model thoughts to Slack via repeated `chat_update`.
    Costs: Slack rate limits on edits, thinking-token latency/cost. Scope as its own phase.

### F6. Dedup message should match the agentic voice (B3 follow-up)
- B3 added a working dedup notice ("📋 This document was already recorded…") but it's a fixed string.
- **Fix:** warm it up + name the doc/month ("📋 I already have **Oct 2025** for this account in your
  bank statement — nothing new to add. Reply 're-process this' to replace it.").

---

## Suggested order
F3 + F4 (wording + naming, quick, high-signal) → F1 (bank Q&A tools) → F2 (dup cleanup + row dedup)
→ F5 pragmatic status line → F6 voice polish → F5 aspirational thought-streaming (separate phase).

Commit each via TDD; restart bot + live-verify after each batch (stale bot = old code).

---

## STATUS — shipped 2026-06-15 (branch `fix/ledgr-hitl-learning-and-qa`)

All of F1–F6 implemented, unit-tested (860 pass), and live-verified in
#akar-enterprises-pte-ltd. Commits: `f5d295a` (F3/F4), `32669c0` (F1),
`01e1ce0` (F2), `11cd576` (F5/F6), `+` opening-balance + tally-noun follow-ups.

- ✅ **F1 bank Q&A tool** — LIVE: "What is the total withdrawal for October 2025?"
  → "The total withdrawal for October 2025 was SGD 4,221.14." (verified vs workbook).
  Added `bank_totals` (withdrawals/deposits/net/opening/closing, month+year filter);
  opening balance is per-period (Oct opens at Sep close), not the sheet's first B/F.
- ✅ **F2 Sept duplication** — LIVE: live Akar FY2025 workbook cleaned 9→4 blocks
  (one B/F per month Jul–Oct, 159→66 rows). `dedupe_blocks` guard added to the merge.
- ✅ **F3 bank ≠ ledger wording** — LIVE: delivery now "Added Nov 2025 (10 transactions)
  to your **Akar Enterprises Pte. Ltd. – Bank Statement FY2025**"; batch tally now
  "posted to your FY2025 bank statement" (was "…QBS Ledger FY2025 ledger").
- ✅ **F4 client-scoped filenames** — LIVE: "Akar Enterprises Pte. Ltd. - BankStatement_FY2025.xlsx".
- ✅ **F5 specific status lines** — LIVE: "🔍 Taking a look at this document…",
  "🏦 Looks like a bank statement — reading each transaction…", etc.
- ✅ **F6 warm dedup voice** — names the month/doc + replace prompt (unit-tested;
  re-upload to see live).

### Still open / deferred
- **F5 aspirational** — true model thought-streaming to Slack (ThinkingConfig +
  chat_update). Deferred to a separate phase (Slack edit rate-limits, latency/cost).
- The Q&A `qa_agent` runs `MODEL_LITE`; if bank-vs-invoice tool routing ever
  misfires on edge phrasing, consider bumping the model or tightening the instruction.
