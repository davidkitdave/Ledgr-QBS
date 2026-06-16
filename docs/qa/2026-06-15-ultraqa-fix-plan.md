# /ultraqa fix-and-re-QA plan — Ledgr intelligent-agent, 2026-06-15

**Goal:** clear the four P0/P1 findings from [`2026-06-15-ultraqa-findings.md`](2026-06-15-ultraqa-findings.md), re-run the same live QA flows that exposed them, and land on a clean gate to start **Phase 4 (write tools)** and **Phase 5 (proactive auto-hints)** next session.

**Branch policy:** stay on `feat/ledgr-intelligent-agent`. No new branches; commit each fix separately with a `fix(ultraqa):` prefix so the timeline reads cleanly.

**Operating mode** (per memory `lead-not-do-delegate-execution`):
- I author TDD specs, ADRs, plan edits, memory writes, and drive the live re-QA.
- Codebase edits + test writing go to an **`executor` sub-agent (Sonnet)** per fix.
- Investigation (read-only) goes to **`Explore` sub-agents** in parallel.
- I verify every executor's diff before moving on (per memory `restart-bot-before-qa` and the "trust but verify" rule).

**ADK grounding** (per memory `ground-fixes-in-adk-mcp-cycle`):
- Round-1 P0-2 investigation MUST query `adk-docs` MCP for the canonical Step-3 chat-tool data-source pattern. The disconnect could be a misuse of session state vs. Firestore. Don't fix until the ADK direction is clear.

---

## Round 1 — Investigation (read-only, parallel, ~5–8 min)

Four `Explore` sub-agents fan out concurrently. Each produces a ≤250-word report with concrete file:line evidence. **No code edits this round.**

### Inv-1 — Locate the COA xlsx extension gate (P0-1)
- Grep for the literal `supported: .gif, .jpeg, .jpg, .pdf, .png, .webp` to find the source list.
- Find the gate's call site in `accounting_agents/slack_runner.py`. Confirm it runs **before** `_is_coa_upload()` (which Phase 0 audit located).
- Report: gate file:line, source-of-truth for the allow-list, the line where `_is_coa_upload()` is reached, and the smallest patch path — preferred shape: "if channel is `pending_coa` AND file ext ∈ {.xlsx, .csv}, skip extension gate."

### Inv-2 — Locate the chat read-tool data source (P0-2)
- List every Step-3 read tool registered on `assistant_agent` (the standalone root, per ADR-0008): expected at `accounting_agents/assistant.py` or sibling.
- For each tool (`summarize_recent_activity`, `list_recent_documents`, `lookup_row`, `explain_categorization`, `explain_tax_treatment`), trace which Firestore path / engine fn it queries.
- Cross-check against where the pipeline WRITES ledger rows: trace from `apply_decision_node` / `consolidate_node` to the actual Firestore write.
- Report: write path vs read path per tool. Highlight any mismatch. Also report the "last 30 days" filter — is it on transaction date or upload date? `summarize_recent_activity` should be transaction-date for accounting questions; `list_recent_documents` should be upload-date.
- **ADK MCP query required:** confirm the canonical Step-3 read-tool data-access pattern for ADK 2.x — should they query session state, Firestore directly, or a service layer?

### Inv-3 — Locate the HITL-approve delivery card branch (P1-1)
- Find the approve-button handler (likely `accounting_agents/slack_runner.py` around `handle_approve` or similar).
- Find the clean-path delivery formatter that emits "Processed N document — N posted... Added X lines to..." + Block Kit table.
- Identify why the approve path returns early or skips the formatter.
- Report: file:line of both branches, smallest diff to call the same formatter from both.

### Inv-4 — Locate the multi-entity approval-gate skip (P1-3)
- Trace from `approval_gate` node to its emit-card logic. When the document is multi-entity (N>1 sub-docs), does it skip the card?
- The JBI run showed the accordion checkmarked "Awaiting approval" but no Approve/Edit/Reject buttons appeared.
- Report: condition that's making the gate auto-pass, file:line, and the smallest fix — most likely: emit ONE summary review card for the multi-entity bundle (not per-sub-doc).

**Gate at end of Round 1:** I review all 4 reports, sequence the fixes, and write per-fix TDD specs for Round 2. If any investigation surfaces a deeper architectural issue (e.g. P0-2 turns out to be a Session vs Firestore confusion that needs an ADR), we pause and decide.

---

## Round 2 — Fixes with TDD (sequential where files overlap, parallel where they don't)

Each fix follows the same pattern, executed by an `executor` sub-agent (Sonnet) with a TDD-spec I author from the Round-1 reports:

1. **Write the failing regression test FIRST** (red).
2. Implement the smallest fix that makes it pass (green).
3. Run fast suite (`tests/`, no integration/eval): must stay ≥ 1366 passed.
4. Run `ruff check` on touched files.
5. Commit `fix(ultraqa): <one-line>` with co-authored trailer.

### Parallelism plan
- **P0-1, P0-2, P1-3** touch different code areas → can run in **parallel** (3 executor agents).
- **P1-1** likely shares files with P0-1 (both in `slack_runner.py`) → run **after** P0-1 commits.

### Fix-1 (P0-1) — COA xlsx upload extension gate

**TDD spec I'll write from Inv-1's report.** Sketch:
- New test `test_coa_upload_xlsx_allowed_when_pending_coa` in `tests/test_slack_runner.py`: simulate a file-share event with `.xlsx` extension when channel status is `pending_coa`. Assert it routes to `_is_coa_upload()` and DOES NOT post the "file type not supported" message.
- Negative test `test_invoice_xlsx_still_rejected_when_not_pending_coa`: same file ext but channel is NOT in `pending_coa` → bot DOES reject (we're not blanket-allowing xlsx).
- After both fail red, executor patches the gate.

**Acceptance:**
- Bot accepts xlsx COA in `pending_coa` state and routes it through `coa_rows_from_file()` → Firestore subcollection populated → bot replies with "Ingested N accounts".
- Bot still rejects xlsx outside `pending_coa`.
- Fast suite ≥ 1366 passes.

### Fix-2 (P0-2) — Chat read tools see freshly-posted docs

**TDD spec I'll write from Inv-2's report.** Likely scenarios depending on Inv-2's finding:
- **If date-filter bug**: test for `list_recent_documents` returns docs uploaded today even if their *transaction* dates are 6 months old.
- **If data-source mismatch**: test that calls the read tool against a Firestore mock pre-populated with the same path the pipeline writes to.
- For `summarize_recent_activity`: if the "last 30 days" filter is on transaction date, that's correct for accounting Q&A — leave it. But test that the tool's empty-result message tells the user the window and how to expand it ("no transactions in the last 30 days; ask me for a specific month or FY for older periods").

**Acceptance (live, not just unit):**
- After Fix-2 lands: in `#rosebery-partner` (data already there from this sweep), ask `@Ledgr-dev list recent documents`. Bot returns the Dec 2025 bank statement. Ask `@Ledgr-dev what's in the ledger for December 2025?`. Bot returns the 4 transactions.

### Fix-3 (P1-1) — HITL-approve path delivery card parity

**TDD spec.** New test in `tests/test_slack_runner.py`:
- `test_approve_path_emits_same_delivery_card_as_clean_path`: simulate a doc going through extract-review (accept) → end-approval (approve) → assert the final Slack message is the same shape as the clean-path delivery (has `posted to your FY{N}` substring + row-count substring + xlsx attachment).
- Mirror against the existing clean-path test if one exists; if not, add one for both to keep them in lockstep going forward.

**Acceptance:**
- Re-run Phase 2B with another Auditair invoice: HITL-approve emits full delivery card.
- Unit test stays green.

### Fix-4 (P1-3) — Multi-entity approval-gate review card

**TDD spec.** New test:
- `test_multi_entity_upload_emits_single_review_card`: simulate a file with N=2 sub-documents extracted. Assert ONE end-approval card is posted naming both sub-docs (not zero, not N).
- Negative: single-entity upload still emits exactly one card (no regression on Fix-3's path).

**Acceptance:**
- Re-run a smaller multi-entity case (don't need full 11pp SOA — can use 2 invoices crammed in one PDF if available, or fabricate one in fixtures).
- Live re-test JBI COOL POWER SOA — approval card appears with the bundle summary.

---

## Round 3 — Local verification (before live re-QA)

After each commit in Round 2:
- `.venv/bin/python -m pytest tests/ --ignore=tests/integration --ignore=tests/eval -q 2>&1 | tail -20`
- Must still report **1366 passed + N new (one per fix) = ≥1370**.
- `ruff check accounting_agents/ invoice_processing/ app/`

Round-3 gate: 4 commits, 4 new tests, suite green, lint clean. If any fail, the executor that wrote that test/fix iterates BEFORE Round 4. We don't move to live re-QA on a yellow build.

---

## Round 4 — Live re-QA (I drive, computer-use Slack ~25 min)

Per memory `restart-bot-before-qa`: kill the dev bot, restart from HEAD, then run **only the flows that exposed the 4 bugs** — surgical, not a full repeat sweep.

| Test | Channel | Expected (post-fix) |
|---|---|---|
| Drop JBI `COA & List.xlsx` | `#jbi-plus-auto` | Bot routes to COA ingest, replies "Ingested N accounts" (Fix-1) |
| Ask `@Ledgr-dev list recent documents` | `#rosebery-partner` | Bot returns the Dec 2025 bank statement (Fix-2) |
| Ask `@Ledgr-dev what's in the ledger for December 2025` | `#rosebery-partner` | Bot returns 4 transactions (Fix-2) |
| Drop another small Auditair invoice (`25-D15-Podaima Paid.pdf`) | `#auditair-international` | Review card → Approve → **full delivery card** with row count + xlsx (Fix-3) |
| Drop JBI COOL POWER SOA again | `#jbi-plus-auto` | Approval-gate review card surfaces ONCE with the 18-doc bundle summary; user approves explicitly; then full delivery card (Fix-4) |

**No regressions check:** the original Phase 2A (Akar bank) and Phase 2C (Rosebery bank) flows must still pass clean — re-drop one statement each if anything in Round 2 touched bank-lane code.

**Round 4 exit criteria:** all 5 cells in the table land green. If any fail, that bug stays P0/P1 and Phase 4/5 stays gated.

---

## Round 5 — Gate Phase 4+5 readiness

Only after Round 4 is green do we open the next session for Phase 4 + 5. Recap of what's needed:

- **Phase 4 (write tools)**: `amend_ledger_row` + `remove_ledger_row` via ADK Tool Confirmation. **Pre-req:** P0-2 fixed (chat read tools see ledger), because amend-by-lookup needs to find the row first. **Pre-req:** P0-1 fixed (so the COA the categorizer reads when re-classifying amended lines is the user-uploaded one, not stale).
- **Phase 5 (proactive auto-hints)**: post-flagged-delivery offer. **Pre-req:** Fix-3 (delivery-card parity) — proactive hint surface attaches to the delivery card; if that card is missing on HITL-approve path, the hint never fires.

If Round 4 is green, I write a brief "go for Phase 4+5" note appended to this plan and reopen the next session with that as the cold-start prompt.

---

## Out of scope this round

- **Steps 9, 10 (full UX), 11, 12** from §7 — these are larger builds the user already deferred. The QA round here only fixes the bug-shaped subset of Step 10 (the extension gate). The full "COA upload step in onboarding modal" + "region/country field" + "drop processing accordion" + "per-channel canvas auto-index" work happens in dedicated future sessions.
- The `summarize_recent_activity` "last 30 days" semantics — if Inv-2 confirms it's correct-by-design (transaction-date-filtered for accounting), we keep it but improve the empty-result message. We DON'T rewrite it to look at upload dates.

---

## Estimated wall-clock

- Round 1 (4 parallel `Explore`): ~8 min
- I read reports + author TDD specs: ~10 min
- Round 2 (3 parallel `executor` then 1 sequential): ~25 min
- Round 3 verification: ~3 min
- Round 4 live re-QA: ~25 min
- Total: **~75 min** to gate Phase 4+5 readiness.

Ready when you give the green light.
