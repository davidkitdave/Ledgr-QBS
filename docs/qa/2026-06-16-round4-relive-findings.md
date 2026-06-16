# Round 4 re-live QA — Ledgr intelligent-agent, 2026-06-16

**Branch:** `feat/ledgr-intelligent-agent` (HEAD `fef4e1c` at end of Round 4 live)
**Bot:** `Ledgr-dev` socket-mode (PID 66648 at end of round; killed once before that)
**Slack workspace:** QBS-AI (dev)
**Reauth event:** user ran `gcloud auth application-default login` mid-round after the bot started 503-timing-out

Source of truth for the four ultraqa fixes that were supposed to land in commits
`43ea1ac / 8784823 / 7539ece / 0d8272b`. Plus the work that surfaced today:
Track A (SOA hard-gate), Track C (Block Kit progress UX research), and the cool-power
SOA ground-truth comparison the developer requested.

---

## 1. Scope

| Test | Channel | Bot at run | Verdict |
|---|---|---|---|
| Restart bot from HEAD | n/a | new PID, then killed once mid-round and re-restarted with Track A live | ✅ DONE TWICE |
| Fix-1: drop `COA & List.xlsx` | `#jbi-plus-auto` | first bot (pre-reauth) | 🟡 NOT VERIFIABLE — channel past `pending_coa` from yesterday; first bot also left a phantom "Processing 1 document..." after the kill |
| Fix-2: `@Ledgr-dev list recent documents` | `#rosebery-partner` | reauth'd bot | ❌ STILL BROKEN |
| Fix-2b: `@Ledgr-dev summarize recent activity` | same | reauth'd bot | 🟡 PARTIAL |
| Fix-3: drop `25-D15-Podaima Paid.pdf` | `#auditair-international` | reauth'd bot | ❌ STILL BROKEN |
| Fix-4: drop `COOL POWER - DEC 2025_.pdf` | `#jbi-plus-auto` | reauth'd bot + Track A | ✅ WORKING (approval card surfaced w/ explicit counts) |
| Track A: SOA hard-gate live | `#jbi-plus-auto` | reauth'd bot + Track A | ⚠️ **LIVE FAILURE** — pytest green, but bot still showed 18 sub-docs / 30 lines (expected 10 / 22) |

---

## 2. P0 findings still open

### P0-A. Bot Firestore RPC dies on expired ADC creds, no surface in chat

**Repro:** Drop the bot binary started at PID 61133 with stale gcloud ADC. Every file upload + every chat query produces no Slack reply. Bot stays alive but every `store.get_by_channel()` 503s and times out after the gRPC retry ceiling (300s).

**Evidence:**
- `/tmp/ledgr-qa/bot.log` showed `google.api_core.exceptions.RetryError: Timeout of 300.0s exceeded, last exception: 503 Getting metadata from plugin failed with error: Reauthentication is needed.`
- The traceback walked from `slack_bolt` → `slack_runner.py:_file_shared:2655` → `client_context.py:get_by_channel:657`.
- During QA, the same auth-needed condition popped a Google passkey dialog when my own Firestore probe tried to read — proving the failure is environmental, not bot-specific.

**Why this matters:** First QA pass attributed Fix-1 + Fix-2 failures to the fixes themselves. With reauth, **Fix-1 became "not testable on this channel"** (status moved past `pending_coa`), but **Fix-2 / Fix-3 are still genuinely broken** — so the new round 4 results below ARE the fix verdicts, not auth artifacts.

**Acceptance:**
- Bot should detect ADC-expired at startup (one liner `db.collection("channels").limit(1).get()` smoke test in `_main_async`) and emit a clear log + a Slack `chat_postMessage` to a dev channel with "Bot reauth needed". Fail-fast > silently hanging on every event.
- Eventual: prod uses service-account creds, so this is mainly a dev-laptop concern. Worth a one-line guard regardless.

### P0-B. `list_recent_documents` STILL can't see fresh ledger rows

**Repro:** In `#rosebery-partner`, a bank statement was processed yesterday at 22:54 UTC+8 (delivery card visible). At 09:45 today, `@Ledgr-dev list recent documents` replies:
> "I'm sorry, but I could not find any recent documents. Please ensure the relevant workbook is uploaded."

**Why this matters:** Commit `8784823 fix(ultraqa): chat session sees freshly-written ledger rows` was supposed to fix exactly this. The bug is unchanged. Either commit fixed a different axis, or its test didn't exercise the real pipeline-write → chat-tool-read path.

**Acceptance (executor sub-agent for `Fix-2 TDD spec` is on this):**
- TDD: a test that writes a fake row to Firestore via the pipeline's real write path, then calls `list_recent_documents` with the same channel/client context — must return that row.
- Empty-message helpfulness: include channel FY + most-recent-row-date in the response.

### P0-C. HITL-approve path STILL doesn't emit the rich delivery card

**Repro:** In `#auditair-international`, dropped `25-D15-Podaima Paid.pdf`. Pipeline → extract reviewer fires (Step 2 working, with a USD/SGD direction clarification) → click "Looks right, keep it" → approval card → click Approve. Bot reply ends with:

> ❌ Bare `"Document processed."`

Compare to clean path (Akar / Rosebery bank): rich delivery with "Processed 1 document — 1 posted to your FY2025 bank statement" + "Added Dec 2025 (4 transactions) to your..." + Block Kit data-table + xlsx attachment.

**Why this matters:** Commit `43ea1ac fix(ultraqa): thread HITL delivery card under the original upload` was supposed to fix exactly this. The bug is unchanged.

**Acceptance (executor sub-agent for `Fix-3 TDD spec` is on this):**
- TDD: a test that simulates reviewer-accept → approval → approve, asserts the final message body matches the clean-path shape (row count + ledger pointer + xlsx).
- Negative: clean-path test stays in lockstep so the two paths never drift again.

### P0-D. SOA hard-gate fires green locally, doesn't fire live

**Repro:** Bot restarted at 09:39 with HEAD `fef4e1c` (commits `b7c4a3b` + `fef4e1c` from Track A loaded). Dropped `COOL POWER - DEC 2025_.pdf` (1 SOA cover + 10 real sub-docs = 22 ground-truth lines). Bot's multi-entity approval card said:

> 📄 COOL POWER - DEC 2025_.pdf
> • 18 sub-documents extracted, 30 total lines — review before posting.

Identical to yesterday. The new `_is_soa_summary_invoice` predicate did NOT drop the 8 phantom SOA-cover rows.

**Three candidate causes (debugger sub-agent is hunting):**
1. **Predicate-miss** — `_is_soa_summary_invoice` requires ALL lines have desc ∈ {"","INVOICE","INVOICES"} AND `gst_amount==0`. The live model may have written `"INVOICE - IA-07316"` (or similar), missing the predicate.
2. **Count built before gate** — The "X sub-documents, Y total lines" string may come from raw `bundle.invoices` before `to_normalized_bundle` runs the gate. Actual ledger rows might be 22, just displayed as 30.
3. **Gate not on this code path** — The multi-entity preview may build from a parallel path that bypasses `to_normalized_bundle`.

**Acceptance:** Root-cause naming which of 1/2/3 it is, plus a regression test that captures the actual live shape (not a clean-room test that happens to pass).

---

## 3. P1 findings still open

### P1-E. Bot crash leaves "phantom processing" message in chat

After killing the first bot (PID 63416), a "📥 Processing 1 document..." message from 09:19 (the COA xlsx upload) stayed in the channel forever — never resolved with "Processed" / "Rejected" / "Failed". No SIGTERM handler updates the in-flight Slack message.

**Acceptance:** Bot startup should grep its own session/log for unresolved status messages and post a follow-up "Bot restarted mid-process — please re-upload this file" to each, or a single SIGTERM/atexit hook that posts the same. Don't leave operators staring at a spinner that will never resolve.

### P1-F. `summarize_recent_activity` empty-window message — partial improvement

Today: *"I am sorry, but I could not find any transactions in the last 30 days. Please specify a different period or check if the ledger is uploaded."*

Yesterday: *"I cannot see any transactions in the channel for the last 30 days."*

The new message names the window ("last 30 days") and offers two suggested actions — improvement. Still missing the most-recent transaction date (which would let the user just ask "show me Dec 2025"). The Fix-2 sub-agent should hit both tools (Fix-2 and Fix-2b) in one pass since the root cause is shared.

---

## 4. Confirmed working — DO NOT regress

| Capability | Phase | Evidence |
|---|---|---|
| Multi-entity approval card (Fix-4) | 2D rerun | Card shows file name, explicit counts ("18 sub-documents extracted, 30 total lines"), and the three action buttons. Reject cleanly aborts ("Document rejected — nothing was added to the ledger"). |
| Step 2 extract reviewer | 2B rerun | Fired on D15 with the same USD/SGD + issuer/bill-to ambiguity the D12 case surfaced — confirming the reviewer is doing real work, not a no-op |
| Onboarding socket-mode → Firestore | 1B | Auditair / Rosebery / JBI channels all carried profile state across the bot restart cycle |
| ADC reauth | n/a | After user re-ran `gcloud auth application-default login`, bot resumed all Firestore reads in <10s; no further 503s |

---

## 5. COOL POWER SOA — ground truth vs yesterday's extraction

Source: `/Users/davidkitdave/Desktop/LocalTest/TestDoc/MYDoc/JBI PLUS AUTO ENTERPRISE/Purchase/COOL POWER - DEC 2025_.pdf` (11 pages, 1.1 MB).

| Page | Content | Expected behavior |
|---|---|---|
| 1 | DEBTOR STATEMENT (SOA cover) — summary of 19 line entries (17 invoices + 1 credit note + 1 payment) | **SKIP** (record in `bundle.skipped_pages`) |
| 2 | CNA-00176 credit note RM 2,552 (6 lines) | extract |
| 3 | IA-07465 RM 385 (1 line) | extract |
| 4 | IA-07467 RM 1,775 (2 lines) | extract |
| 5 | IA-07514 RM 586 (2 lines) | extract |
| 6 | IA-07522 RM 420 (2 lines) | extract |
| 7 | IA-07526 RM 400 (1 line) | extract |
| 8 | IA-07527 RM 180 (2 lines) | extract |
| 9 | IA-07573 RM 645 (2 lines) | extract |
| 10 | IA-07588 RM 2,160 (3 lines) | extract |
| 11 | IA-07590 RM 225 (1 line) | extract |

Ground truth = 10 sub-documents (1 credit note + 9 invoices), 22 ledger lines.

Yesterday's extraction (`/Users/davidkitdave/Downloads/JBI Plus Auto Enterprise - Ledger_FY2025.xlsx`):
- 30 rows
- 8 **phantom** rows hallucinated from the SOA cover summary table:
  `IA-07316`, `IA-07330`, `IA-07332`, `IA-07365`, `IA-07368`, `IA-07383`, `IA-07392`, `IA-07428` — all with `description="INVOICE"`, tax=0, sub_total==total, no item code
- 22 **real** rows from pages 2-11 (the credit note's 6 negative lines + the 9 invoices' 16 positive lines)
- Currency wrong everywhere: showed SGD on a Malaysian RM doc (Step 9 region/country split is NOT STARTED — known)
- `Account Code / COA` empty on every row — but that's NOT a wiring bug; the categorizer IS wired to client-COA (see §6 below). It's empty because the COA xlsx never ingested (Fix-1 bug).

---

## 6. COA wiring — verified, NOT a bug

Explore sub-agent confirmed (read-only investigation):

- COA persisted at Firestore `clients/{client_id}/coa/{n}` by `FirestoreClientStore.save_coa()` ([client_context.py:678-693](invoice_processing/export/client_context.py:678))
- Categorizer reads via `coa_from_state(ctx.state)` ([nodes.py:411-443](accounting_agents/nodes.py:411), [client_context.py:264-275](invoice_processing/export/client_context.py:264))
- Resolution order: entity_memory → category_mapping → COA keyword match → LLM with client COA
- LLM prompt explicitly says "COA (choose key from these only):" ([categorizer.py:184-185](invoice_processing/export/categorizer.py:184))

**Verdict:** WIRED. The reason yesterday's JBI ledger had no account codes is that the COA xlsx was rejected at the extension gate before `run_coa_ingest` could run. Once Fix-1 is verified on a fresh `pending_coa` channel, the categorizer will populate the column.

---

## 7. Block Kit progress-UX recommendation (Track C)

Explore sub-agent surveyed `app/blocks.py` + Slack docs:

- Current shape: `_post_status()` posts one message with `processing_plan_blocks()` (native `plan` block: Classify → Extract → Categorize → Tax → Awaiting approval). 5–8 `chat_update` calls per run, no rate-limit risk.
- Gap: a 11-page bundle sits on "🧾 Extracting" for 2+ minutes with no per-doc progress.
- **Recommendation:** keep the `plan` block, add a native `task_card` carousel BELOW it. Each card = one sub-doc (vendor name + invoice number + status emoji). Rotate every 30–60 sec via `chat_update` (3–5 edits per run, well under rate limit). Both blocks already exist in `NATIVE_BLOCK_TYPES` so the fallback is one helper.
- Effort: ~200 lines (carousel builder in `app/blocks.py`, hook into the extract-node event loop).
- Does NOT conflict with §0.5-I / §7 Step 12 (the "drop the accordion" plan): the accordion answers "what stage", the carousel answers "what doc". They co-exist meaningfully.

To pick up later. Not built this round.

---

## 8. Tracks running at end of round (background sub-agents)

1. **Track A debug** (`oh-my-claudecode:debugger`) — root-cause the SOA hard-gate live failure, ship a tighter regression that captures the real shape.
2. **Fix-2 TDD + impl** (`oh-my-claudecode:executor`) — failing test + real fix for `list_recent_documents` + most-recent-date hint for `summarize_recent_activity`.
3. **Fix-3 TDD + impl** (`oh-my-claudecode:executor`) — failing test + real fix for HITL-approve delivery card parity vs clean path.

After all three return, restart the bot and re-run the 5-test surgical sweep in Slack from a CLEAN state (Akar / Auditair / Rosebery / fresh JBI channel for Fix-1).

---

## 9. Plan-doc updates needed after this round

Append to `docs/qa/2026-06-15-ledgr-intelligent-agent-masterplan.md`:

- §0.5 add J — Round 4 re-QA showed that 3 of the 4 P0/P1 fixes from yesterday (Fix-2 chat reads, Fix-3 HITL delivery, Fix-1 COA reachability) did NOT actually fix their bugs. The fixes shipped passed unit tests but missed the real-runtime path. **Implication:** the eval/fixture coverage for these areas is too narrow — TDD specs must exercise the actual pipeline → chat-tool path, not isolated unit slices.
- §11 (anti-goals) add — **"Not passing pytest-green and shipping without live verification."** The Track A green-locally / fail-live finding means every "fix" needs at least a smoke against a real PDF + a real Slack channel before merging. Add this to the masterplan's §10 verification gate.

Memory write candidates (after this round closes):
- `live-fix-must-replicate-real-shape` (feedback) — fixes that pass pytest with clean-room fixtures but don't fire on real model output are not fixes. Always include a real-shape regression test alongside.
- `bot-needs-startup-firestore-smoke` (feedback) — ADC expiry on dev laptop silently kills the bot via 300s RPC timeouts. One-line Firestore probe on `_main_async` start would fail-fast.
