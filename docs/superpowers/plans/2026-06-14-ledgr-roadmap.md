# Ledgr — Roadmap (index)

The single map. Two execution plans, in order, plus parked future work. When two
documents seem to disagree, this index + the ADRs win.

## The runtime, in one line (corrected 2026-06-14)
The **live bot is the `accounting_agents` ADK-2.0 graph** (started via
`accounting_agents.slack_runner.main`), whose nodes call the deterministic engine in
`invoice_processing/`, and which reuses `app/` for onboarding/commands/blocks. The old
`app/processing.py` (`process_batch`) path + `ledgr_coordinator/` are the **dead
duplicate** to retire. (ADR-0001 + its 2026-06-14 addendum.)

## Plans, in order

1. **Plan A — Fixes** · `2026-06-14-ledgr-template-onboarding-hitl-fixes.md` · **do first.**
   Honour the Xero/QBS template (no silent QBS default), confirm the registered profile,
   and make HITL real: name the file on review cards, a working **Edit** modal, one
   threaded **Job** summary instead of channel spam. (ADR-0003/0005/0007.)

2. **Plan B — Extraction accuracy** · `2026-06-14-ledgr-extraction-accuracy.md` · **do second.**
   Make the numbers correct, each gated by the eval: COA **placement** accuracy (+ HITL),
   sales-vs-purchase/vendor robustness, multi-receipt/multi-currency split + FX,
   discount/dropped-tax reconciliation, missing invoice date/number, GST determinacy,
   the **Akar bank accumulation fix + one-time repair**, reject unreadable uploads.
   (ADR-0004/0005/0006.)

## Housekeeping (small, not a plan)
- ✅ **ADR-0001 corrected** — graph is live; retire the `process_batch` duplicate + `ledgr_coordinator`.
- ☐ **Unify Cloud Run** — `app/main.py` (FastAPI) still runs the old `process_batch` path;
  switch it to drive the same graph as the socket runner before deploy.
- ☐ **Delete dead duplicate** — `app/processing.py`, `app/socket_run.py`,
  `app/slack_app`'s own `build_app`/file-share path, `ledgr_coordinator/`, and the unused
  ADK-1.x fork code in `invoice_processing/` (`agent.py`, `sub_agents/`,
  `shared_libraries/acting`+`investigation`+`alf_engine`). Verify nothing live imports them, keep tests green.

## Parked (valid, not yet scheduled — from the 2026-06-13 grilling)
- **Teammate Q&A** — the Coordinator answers "what's my GST / did you get January /
  what's missing" by reading the channel's workbook; proactive gap detection.
- **Slack-native storage + FY Canvas index** — channel Files tab as the system of
  record; re-point prior-workbook retrieval off GCS; per-FY Canvas "folder view".
  (ADR-0002.)

## Eval = the scoreboard (built 2026-06-14)
- `eval/client_eval.py` — per-client invoice eval: loads each Cast Unity `Client
  Setup.xlsx` (identity + own COA) + Sys_Config profile; scores **direction vs the
  Sales/Purchase folder** and **per-target required-header completeness** (QBS + Xero).
- `eval/bank_eval.py` — bank statements vs `BankStatement_FY` ground truth.
- **Baseline (48 docs, 8 clients):** classify 100%; direction **60%**; reconciliation
  87% (Auditair 0%); invoice no./date ~92–93%; Account-Code fill 85% (blank-by-design
  for no-COA clients — measure *placement*, Plan B Task 1).

## ADR map
0001 deterministic engine + slim graph (+ 2026-06-14 runtime correction) ·
0002 Slack as system of record + FY Canvas · 0003 HITL via `RequestInput` ·
0004 learning via Corrections (not Memory Bank) · 0005 canonical schema + per-target
projection + completeness contract (+ target-resolution addendum) ·
0006 per-client COA onboarding (soft gate) · 0007 HITL review/edit Slack surface.
