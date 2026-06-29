# Credit system — live QA checklist

**Spec:** [docs/adr/0016-credit-deduction-and-manual-topup.md](../adr/0016-credit-deduction-and-manual-topup.md)  
**Requirement matrix:** ADR-0016 §2–§5 (implemented slices) + §6/§7 (App Home / identity — pending)

Run after each implementation slice and before Northwind Advisory (or any contract customer) go-live.

> **Reconciled 2026-06-29 (ADR-0016 governs).** Core billing is shipped: gate at
> upload, charge on delivery, `FirestoreCreditStore`, dev grants via
> `LEDGR_DEV_CREDIT_GRANTS`. Sections **A–C** (invites, probes, auto-grant) and
> parts of **F/H** (App Home, %-alerts) are **not implemented** — skip or treat as
> future work. Use **G** + **D** for the current go-live gate.

## Prerequisites

- [ ] Cloud Run (or socket bot) running with OAuth env vars (`SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_BASE_URL`, `SLACK_OAUTH_STATE_SECRET`)
- [ ] App Home tab ON (api.slack.com → Ledgr → App Home)
- [ ] `app_home_opened` in manifest bot events
- [ ] `LEDGR_OPERATOR_CHANNEL_ID` set
- [ ] Test PDFs: single invoice, multi-invoice PDF, 3-page bank statement, duplicate of prior drop

## Automated gate (run first)

```bash
uv run pytest tests/test_credit_service.py tests/test_credit_store_firestore.py \
       tests/test_credit_delivery.py tests/test_durable_credit_wiring.py \
       tests/test_dev_credit_grants.py tests/test_credit_fail_open_policy.py -q
```

With clean-agent Slack wiring (D.2):

```bash
uv run pytest tests/test_clean_agent_slack_dispatch.py tests/test_d1_clean_path_e2e.py -q
```

---

## QA-A — Install & firm bootstrap (Slice 2) → R1, R2, R3

> **Not implemented** — no install-invite / auto-grant flow. Skip until §7 identity
> capture ships. For now, grant manually (see QA-G).

| # | Step | Expected | Pass |
|---|------|----------|------|
| A1 | `create_install_invite.py --ref QA-TEST-001 --credits 100 --label "QA Firm"` | `install_invites/QA-TEST-001` in Firestore | ☐ |
| A2 | Open `…/slack/install?ref=QA-TEST-001` → Allow | Bot installed | ☐ |
| A3 | Firestore `firms/{Uxxx}` | `invoice_ref=QA-TEST-001`, `status=active` | ☐ |
| A4 | `firms/{U}/credits/balance` | `total_credits=100` | ☐ |
| A5 | `workspaces/{key}` | `firm_id` = installer `user_id` | ☐ |
| A6 | Welcome DM | Credits ready + open Ledgr sidebar | ☐ |
| A7 | Operator channel | Install + auto-grant confirmation | ☐ |
| A8 | `list_firms.py --ref QA-TEST-001` | ACTIVE, 100 credits | ☐ |

---

## QA-B — Pay-first (Slice 2) → R4

> **Not implemented** — skip.

| # | Step | Expected | Pass |
|---|------|----------|------|
| B1 | Install without `?ref=` | `pending_credits`, balance 0 | ☐ |
| B2 | Tap **Request activation** | Operator channel card | ☐ |
| B3 | `grant_credits.py --user Uxxx --amount 50` | Balance 50, active, activation DM | ☐ |

---

## QA-C — Probe & gate (Slice 3–4) → R5, R6

> **Partially implemented** — gate uses page-count ceiling (no Gemini probe). Skip
> probe-line expectations; verify gate at `balance ≤ 0` and page-count refusal.

| # | Step | Expected | Pass |
|---|------|----------|------|
| C1 | Balance 0 | — | ☐ |
| C2 | Drop PDF | Insufficient card, no processing | ☐ |
| C3 | Grant 20 credits | Balance restored | ☐ |
| C4 | Drop single invoice | Probe line before pipeline | ☐ |
| C5 | Drop 3-page bank PDF | ~3 credits estimate | ☐ |
| C6 | Batch 2 files | Job header sums estimates | ☐ |
| C7 | Balance < estimate | Blocked, no Gemini spend | ☐ |

---

## QA-D — Delivery charge (Slice 4) → R7, R8, R9, R13

| # | Step | Expected | Pass |
|---|------|----------|------|
| D1 | Invoice delivered | Footer: Used 1 credit · N remaining | ☐ |
| D2 | `creditLedger` entry | committed, channel_id, balance_after | ☐ |
| D3 | Re-drop dedup | 0 charge | ☐ |
| D4 | Bank 3 pages delivered | Charge = pages, not 1 | ☐ |
| D5 | Batch 2 invoices | Per-file charge; job total | ☐ |
| D6 | Retry delivery | No double charge | ☐ |

---

## QA-E — Teammate vs owner (Slice 2+4) → R10

| # | Step | Expected | Pass |
|---|------|----------|------|
| E1 | Teammate drops PDF | Processes | ☐ |
| E2 | After delivery | Installer balance debited | ☐ |
| E3 | Ledger | `uploaded_by` = teammate | ☐ |

---

## QA-F — Credit visibility (Slice 5) → R11, R12

> **Not implemented** — App Home / `/ledgr credits` / %-bar pending. Delivery-card
> footer (`Used N credits · M remaining`) is the current visibility surface.

| # | Step | Expected | Pass |
|---|------|----------|------|
| F1 | Ledgr → Home tab | Balance + by-channel breakdown | ☐ |
| F2 | Process doc | Home refreshes or Refresh works | ☐ |
| F3 | `/ledgr credits` in DM | Ephemeral master card | ☐ |
| F4 | `/ledgr credits` in client channel | Channel + account view | ☐ |
| F5 | Full history button | Modal with ledger | ☐ |
| F6 | Request top-up | Modal → operator channel | ☐ |
| F7 | `/ledgr help` | Lists credits | ☐ |
| F8 | App Home account ID | `team_id` shown as "Ledgr account ID" (quote on payment) | ☐ |
| F9 | App Home %-remaining bar | Bar matches `balance / cycle_start` against 50/25/10 marks | ☐ |

---

## QA-G — First production firm dry run → R14

Manual-grant flow (no invites/auto-grant). Use the firm's real `team_id` from `list-firms`.

| # | Step | Expected | Pass |
|---|------|----------|------|
| G1 | `accounting_agents.admin list-firms` (prod) | Firm appears with installer name; balance 0 | ☐ |
| G2 | `accounting_agents.admin grant --firm <team_id> --amount <trial> --note "trial"` | Firm record created; balance = trial; `topup` ledger row | ☐ |
| G3 | One client channel + COA + invoice | Credit footer on delivery; balance decremented | ☐ |
| G4 | App Home | Balance matches ledger; per-client usage + account ID shown | ☐ |
| G5 | `accounting_agents.admin list-firms` | Balance reflects usage | ☐ |

---

## QA-H — Low-balance alerts (Slice 4) → R16

> **Not implemented** — %-of-last-top-up DMs and operator mirrors are still open.

%-of-last-top-up model: denominator = `cycle_start` (balance after the most recent top-up).

| # | Step | Expected | Pass |
|---|------|----------|------|
| H1 | Grant 100 | `credits.cycle_start = 100`, `alerts_sent = []` | ☐ |
| H2 | Spend to 49 (<50%) | One DM to installer ("50% left"); `alerts_sent=[50]` | ☐ |
| H3 | Spend to 24 (<25%) | DM "25% left"; 50% not re-sent | ☐ |
| H4 | Spend to 9 (<10%) | DM installer **and** mirror to operator channel | ☐ |
| H5 | Spend to 0 | "Depleted" DM + operator mirror; gate now refuses new drops | ☐ |
| H6 | Top up +100 | `cycle_start = 100`, `alerts_sent = []` (re-armed) | ☐ |

---

## Sign-off

```
Date:
Tester:
Environment: [dev / Cloud Run URL]
Bot: [Ledgr-dev / Ledgr]
Slices: [1 / 2 / 3 / 4 / 5]

Sections: A__ B__ C__ D__ E__ F__ G__ H__
Blockers:

[ ] Ready for first production firm   [ ] Needs fixes
```
