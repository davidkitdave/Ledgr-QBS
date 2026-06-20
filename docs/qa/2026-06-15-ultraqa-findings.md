# /ultraqa findings — Ledgr intelligent-agent live QA, 2026-06-15

**Branch:** `feat/ledgr-intelligent-agent` (HEAD `34f25fd`)
**Bot:** `Ledgr-dev` socket-mode, `LEDGR_ENV=dev`, LEDGR-DEV workspace
**Fast test suite at start:** 1366 passed in 5.82s

This doc is the deliverable from the live /ultraqa sweep that complements the planning doc
`2026-06-15-ledgr-intelligent-agent-masterplan.md` (the masterplan added §0.5-H and Steps 9–12 in
this same session). Findings below are evidence-first; every bug includes a reproduction and a
file:line hint for the executor where I have one.

---

## 1. Scope

| Phase | Status | What ran |
|---|---|---|
| 0 — code audit (6 parallel reads + fast suite) | ✅ DONE | Socket-mode wiring, channel lookup, Slack canvas/folder, region/COA gaps, COA upload path, test suite |
| 1A — restart bot from HEAD | ✅ DONE | `LEDGR_ENV=dev` socket-mode runner up |
| 1B–1E — onboard 4 test channels | ✅ DONE | `#sample-bank-client`, `#acme-client-test`, `#sample-partner`, `#sample-auto-enterprise` with real Sample Test Group names + setup values |
| 2A — Sample Bank Client bank statement (digital PDF) | ✅ DONE | Dec 2025 statement processed, full delivery card |
| 2B — Acme Client purchase invoice | ✅✅⚠️ | Step 2 reviewer + end approval fired; HITL-approve path missing delivery card |
| 2C — Sample Partner bank statement (scanned PDF) | ✅ DONE | 4-tx closing-account month, vision lane working |
| 2D — Sample Auto Enterprise COA upload + Sample Vendor Inc SOA | ❌+✅ | COA upload BROKEN at extension gate; SOA split 1 PDF → 18 docs → 30 lines |
| 3 — chat agent read tools | ✅+❌ | Chat replies in thread; read tools don't see processed docs |
| 4 — write tools (amend/remove + Step 7 chat→engine) | ⏸ DEFERRED | Gated by P0-2 below |
| 5 — proactive auto-hints | ⏸ DEFERRED | Built on Step 7, also gated |

---

## 2. P0 findings (block ship-readiness)

### P0-1. COA xlsx upload rejected by extension allow-list, despite bot UX promising xlsx/csv

**Reproduction:** Phase 2D, `#sample-auto-enterprise`.
After onboarding, bot posts: *"✅ Profile saved. Drop your COA file (.xlsx/.csv) here, or tap Use
standard SG SME COA"*. Dropping `COA & List.xlsx` (29 KB, valid Excel) gets:

> *"Sorry, I couldn't read `COA & List.xlsx` — the file type is not supported (got `.xlsx`;
> supported: .gif, .jpeg, .jpg, .pdf, .png, .webp). Please re-upload a supported document
> (PDF, PNG, JPG, WEBP, or GIF)."*

**Why this matters:** Step 10 plan assumed `_is_coa_upload()` already routed xlsx during
`pending_coa`. The Phase 0 audit confirmed the routing *code* exists. But a **runtime extension
allow-list runs BEFORE the COA-upload check**, killing xlsx files before they can route to
`coa_rows_from_file()`. The bot literally tells users to drop xlsx and then rejects what they drop.

**Likely fix locations** (need executor verification):
- `accounting_agents/slack_runner.py` — find the supported-extensions gate (search for the
  literal `supported: .gif, .jpeg, .jpg, .pdf, .png, .webp` or its source list)
- The fix is **EITHER** allow `.xlsx`/`.csv` unconditionally **OR** allow them only when the
  channel is in `pending_coa` status. The latter is safer and matches Step 10's gate.

**Acceptance criteria:**
- Bot's "drop your COA file (.xlsx/.csv)" message can be acted on — upload an xlsx COA → routes
  to ingest → Firestore profile's COA subcollection populated → bot confirms count of accounts
  ingested.
- Regression test: same channel can still process invoice PDFs normally after COA ingest.

### P0-2. Chat read tools don't see documents the pipeline just posted

**Reproduction:** Phase 3, `#sample-partner`.
1. Uploaded `2025 12.pdf` Sample Partner scanned bank statement. Bot processed cleanly, posted:
   *"✅ Processed 1 document — 1 posted to your FY2025 bank statement"* + xlsx preview.
2. ~1 min later, in same channel: `@Ledgr-dev summarize the recent activity in this channel`.
   Bot replied **in thread** (Step 1 multi-turn / ADR-0008 working): *"I cannot see any
   transactions in the channel for the last 30 days."*
3. Turn 2: `what documents have been processed in this channel?`. Bot replied: *"I cannot see
   any documents that have been processed in this channel. Please upload a file."*

The bot literally claims no documents exist in a channel where it just posted a delivery card 60
seconds earlier.

**Two possible causes (executor to investigate):**
- **A: "Last 30 days" filter vs ground-truth dates.** Test data is Dec 2025; system clock is
  2026-06-15. Tools like `summarize_recent_activity` and `list_recent_documents` may filter by
  transaction date, which is correct for "last 30 days" — but **the chat tool that lists
  *processed documents* should be filtered by upload timestamp, not transaction date**. Verify
  the tools in `accounting_agents/assistant.py` (or wherever Step 3 tools live).
- **B: Data-source mismatch.** The pipeline writes to a Firestore path the chat tools don't
  read. e.g. ledger rows in `clients/{client_id}/ledger/...` vs the tool reading from
  `clients/{client_id}/documents/...`. Step 3 audit suggested the tools use the same engine
  code — confirm at runtime.

**Why this matters:** P0-2 cascades to Step 4 (amend by lookup needs to find the row first) and
Step 7 (`replace_recorded_month`, `re_extract_document` both depend on finding the doc/row).
**Phase 4 testing was deferred because of this** — write tools likely hit the same disconnect.

**Acceptance criteria:**
- Drop a doc → wait for delivery card → ask `@Ledgr-dev list recent documents` → bot returns
  the doc just uploaded.
- `summarize_recent_activity` either drops the "last 30 days" filter or expands to fiscal year
  / asks for a date range when the channel has older docs.

---

## 3. P1 findings (UX bugs, real but not blocking)

### P1-1. HITL-approve path delivers "Document processed." with no row count / no ledger pointer / no Block Kit table

**Reproduction:** Phase 2B, `#acme-client-test` with `INV-2025-012-sample.pdf`.

Sequence the bot posted:
1. *"🚨 Processed 1 document — 1 needs your review"*
2. Extract-reviewer card (Step 2 mid-flow HITL) → user clicks "Looks right, keep it"
3. *"✅ Extraction accepted — continuing."*
4. End-approval card (ADR-0007) → user clicks "Approve"
5. *"✅ Approved."*
6. *"Document processed."*

Compare to the clean path (Phase 2A Sample Bank Client bank): step 6 is replaced by a rich delivery card
("Processed 1 document — 1 posted to your FY2025 bank statement" + "Added Dec 2025 (4
transactions)..." + xlsx preview).

**Hypothesis:** the delivery-card builder runs on the clean-path branch only; the HITL-approve
branch returns early after committing the row, skipping the formatter. Likely
`accounting_agents/slack_runner.py` around the approval-gate handler.

**Acceptance:** HITL-approve and clean-path end with the same delivery card shape (row count,
ledger pointer, Block Kit data table) — the approve action implies "this row is now in the
ledger; show me the ledger."

### P1-2. Per-step processing-status accordion is redundant with the chat agent (already in plan as Step 12)

Captured as §0.5-I and §7 Step 12 in the masterplan during this session; saved as memory
`delivery-surface-trim-redundant-status`. Listed here for completeness.

### P1-3. Approval gate not visible on the Sample Auto Enterprise Sample Vendor Inc SOA path

**Reproduction:** Phase 2D, `#sample-auto-enterprise`, Sample Vendor Inc PDF.

The processing accordion checkmarked "Awaiting approval" but I never saw an
Approve/Edit/Reject card — pipeline auto-posted 30 lines from 18 docs straight to the Sample Auto Enterprise
Ledger FY2025. Compared to Phase 2B where the same gate fired correctly with a card.

**Likely cause:** when N>1 documents/lines are extracted from one upload, the per-doc approval
gate may be silently skipped or auto-passed. Verify the approval-gate node's behavior when
processing a multi-entity result vs a single-entity result.

**Why this matters:** the user's whole "I'm a junior accountant" promise depends on never
silently posting wrong numbers. If a 30-line SOA can skip review, a 30-line error can land in
the books unreviewed.

### P1-4. Sample Auto Enterprise multi-entity output is SGD on every line (MY company)

**Reproduction:** Phase 2D, Sample Auto Enterprise ledger preview shows `Currency: SGD` on every Sample Vendor Inc line.

Expected — Step 9 (region/country branch) is `⬜ NOT STARTED` per masterplan §7. Flagged here as
the live confirmation that MY clients need Step 9 before going live.

---

## 4. Confirmed working — DO NOT regress

| Capability | Phase | Evidence |
|---|---|---|
| Onboarding wizard end-to-end | 1B–1E (4× in a row) | Real Sample Test Group values accepted, Firestore profile written, `pending_coa` state set |
| Socket-mode → Firestore wiring | 0 + 1B | `_DEFAULT_CLIENT_STORE = FirestoreClientStore()` ([slack_runner.py:99](accounting_agents/slack_runner.py:99)); profile lookup at `channels/{channel_id}` matches between write ([app/slack_app.py:306](app/slack_app.py:306)) and read ([client_context.py:649-664](invoice_processing/export/client_context.py:649)). Memory `socket-mode-store-split-followup` is **stale — bug closed**. |
| Bot auto-onboarding card on `member_joined_channel` | 1B | Bot posts "Welcome to Ledgr!" + "Set up this client" button the instant it joins |
| Step 1 chat lane (standalone root + per-thread session) | 3 | Bot replied in a thread, not as another `Ledgr-dev APP` reply on main timeline — confirms ADR-0008 design landed in code |
| Step 2 extract reviewer (mid-flow HITL) | 2B | Reviewer fired on REAL ambiguity: *"document currency is USD and base currency is SGD; could not determine if client is issuer or bill-to"* with Looks-right / Re-extract-with-hint buttons. **Major positive finding.** |
| End approval gate (ADR-0007) | 2B | Approve/Edit/Reject card posted after reviewer accept, registered click cleanly |
| Multi-entity extraction (memory `multi-entity-extraction-requirements`) | 2D | 11pp SOA → 18 unique sub-documents → 30 ledger lines |
| Bank lane — digital PDF | 2A | Sample Bank Client Dec 2025 statement → full ledger row table with Date / Description / Withdrawal / Deposit / Balance / Currency / Math_Check (✓ all rows) |
| Bank lane — scanned/vision PDF | 2C | Sample Partner Dec 2025 closing-account month, 4 transactions including closing balance + interest credit |
| FY ledger continuity (`Math_Check`) | 2A, 2C | Memory `bank-ledger-continuous-sorted` rule visible as a column with green ticks |
| §0.5-C master gate — non-GST-registered onboarding | 1B/1C/1D | All three SG clients onboarded with `GST status: Not GST-registered`, confirmed by bot's registration card |
| Block Kit delivery preview (the work just landed on this branch) | 2A, 2C, 2D | The "OCBC SGD — last N rows added" / "Sample Auto Enterprise - Ledger_FY2025.xlsx" preview tables render correctly |

---

## 5. Plan-doc updates landed in this session

| Change | Where |
|---|---|
| §0.5-H — QA scope + MY/COA gaps + Slack folder discovery | masterplan §0.5 |
| §0.5-I — Delivery surface duplication finding | masterplan §0.5 |
| §7 Step 9 — Region/country split + non-SG tax | masterplan §7 |
| §7 Step 10 — COA from user upload, not seeded default | masterplan §7 |
| §7 Step 11 — Per-channel Canvas as auto-index (re-scoped from "folder per channel") | masterplan §7 |
| §7 Step 12 — Drop redundant processing-status accordion | masterplan §7 |

Memory writes in this session:
- `delivery-surface-trim-redundant-status` (feedback) — Step 12 rationale + how-to-apply

---

## 6. Recommended executor handoff

Order suggested for fix work, based on dependency:

1. **P0-1** COA upload extension gate — small, surgical fix, unlocks Step 10 live testing.
2. **P0-2** Chat read-tool ↔ ledger disconnect — investigate first (is it the date filter or the
   data-source path?), then fix. Unlocks Phase 3/4 verification.
3. **P1-1** HITL-approve delivery card parity — one branch needs the same formatter as the clean
   path.
4. **P1-3** Approval gate on multi-entity uploads — confirm the masterplan §7 Step 2 guarantee
   ("≤2 reviews + ≤1 re-extract bound" still includes the end approval) — a 30-line silent
   post is a worse failure than an in-progress reviewer pass.
5. **Step 9** (region/country) and **Step 10** (COA upload UX) — the largest items still ⬜
   NOT STARTED in §7; gate Sample Auto Enterprise/MY live testing.

After 1–4 land, re-run **Phase 4 (write tools) and Phase 5 (proactive auto-hints)** which were
deferred this session.

---

## 7. State for the next session

- Dev bot (PID at writing time): still running socket-mode from HEAD. Memory `restart-bot-before-qa`:
  next session restart before testing.
- 4 dev channels exist in LEDGR-DEV workspace: `#sample-bank-client`, `#acme-client-test`,
  `#sample-partner`, `#sample-auto-enterprise`. Real Sample Test Group names. Reusable for next QA round —
  don't recreate.
- Firestore dev profiles + ledger rows from this sweep are present. If you want a clean reset
  before next round, delete the channels' Firestore docs OR pick fresh channel names.
- This findings doc + the masterplan changes are uncommitted on `feat/ledgr-intelligent-agent`.
