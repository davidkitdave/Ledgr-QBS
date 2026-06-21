# Ledgr-QBS — Slack Credit System (execution plan)

**Date:** 2026-06-20
**Status:** Approved — ready for slice-by-slice build + QA.
**Governs:** This plan is the **authoritative, execution-ready** doc. It reconciles
the older design spec with the billing ADR.

- **Decision record:** [ADR-0016 — Credit deduction and manual top-up](../../adr/0016-credit-deduction-and-manual-topup.md) (see its 2026-06-20 amendment).
- **Design spec (partly superseded):** [specs/2026-06-17-slack-credit-system-design.md](../specs/2026-06-17-slack-credit-system-design.md) — read its Reconciliation banner first.
- **Live QA checklist:** [docs/qa/credit-system-live-qa-checklist.md](../../qa/credit-system-live-qa-checklist.md)

Where this plan, the spec, and ADR-0016 differ, the order of authority is
**this plan → ADR-0016 → spec**.

---

## Why this plan exists

A `/grill-with-docs` session (2026-06-20) found the spec and ADR-0016 **contradicted
each other** on the most load-bearing decision — *what a Firm is* — plus several
downstream mechanics. The session resolved every branch and **verified the identity
mechanism live** (`users.info` on the stored per-workspace bot token resolves real
installer names for all existing installs, with the `users:read` scope already held —
no reinstall). This plan is the settled result.

---

## The locked design (one table)

| Stage | Decision |
|-------|----------|
| **Firm** | Firm = **Slack workspace `team_id`** (`T…`). The **installer** (`U…` + name) is **identity metadata only**, never the key. One firm = one workspace = one balance shared across all client channels. |
| **Identity** | Name via `users.info` (verified live). `users:read.email` added for **new** installs only; **never force a reinstall**. The `team_id` is surfaced as the firm's **"Ledgr account ID"** to quote on invoices — the robust invoice↔firm binding. |
| **Firm record** | `firms/{teamId}` created **lazily** by an idempotent `ensure_firm(team_id)`. The **`grant` CLI calls it**, so granting an already-installed firm creates the record on demand. **No OAuth-success hook, no backfill script.** |
| **Grant / top-up** | One CLI: `python -m accounting_agents.admin grant --firm <team_id> --amount N [--ref INV-… | --note "trial"]`. A **trial is just a grant** with `--note "trial"` (optional `expires_at`). Transactional (balance + ledger row). `list-firms` lists every install + balance. Run from laptop against **prod** (ADC; `LEDGR_FIRESTORE_NAMESPACE` unset). |
| **Gate** | At file-event entry: `balance ≤ 0` → refuse; then **page-count ceiling** (`pypdf` page count `> balance`) → refuse. **No Gemini probe.** Mid-batch overrun: process until balance hits 0, then skip remainder and report delivered-vs-skipped. |
| **Charge (on delivery only)** | **Invoice / receipt / expense-claim / other = 1 credit per unique document written** (the `appended` count). **Bank = every source-PDF page** (the page count from the gate, reused). Deduped = 0, rejected = 0, re-extract / replace-in-place = 0. |
| **Idempotency** | Deduction keyed by `file_id` (`{channel_id}:{file_id}:deliver`). Batch: deduct from the **flush result** of `_flush_deferred_ledger_writes` (per group), never the per-doc payload (which is `appended=0` pre-flush). |
| **Visibility — Tier 1** | Delivery footer (`Used N · M remaining`) · `/ledgr credits` (ephemeral; master view in DM, channel view inside `#client`) · firm low-balance DM. |
| **Alerts** | **% of last top-up.** Denominator = `cycle_start` (balance right after the most recent top-up). DM installer once each at **50 / 25 / 10 / 0%**, debounced via `alerts_sent`. Top-up **resets** `cycle_start` + clears `alerts_sent`. **10% and 0% also mirror to `LEDGR_OPERATOR_CHANNEL_ID`** (pushed operator follow-up). |
| **Visibility — Tier 2 (App Home)** | Enable Home tab + `app_home_opened` (config + event, **no new scope → no reinstall**). Hero balance + 50/25/10 %-remaining bar + **per-client usage** + recent activity + account ID + Request-top-up (posts to operator channel). |
| **QA gate** | Run QA-A→H on **dev** (QBS-AI / David-Workspace) → green → only then grant the first production firm on prod. |

---

## Firestore schema (final)

Namespace via `_ns()` (prod = unprefixed, dev = `dev_`).

```
firms/{teamId}                              # teamId = Slack workspace team_id (T…)
  team_id, team_name
  installer_user_id, installer_name
  installer_email?                          # only once users:read.email is granted
  installed_at, source?                     # e.g. "trial" | "paid"

firms/{teamId}/credits                      # single doc
  balance:      int                         # live balance
  cycle_start:  int                         # balance immediately after the most recent top-up (alert denominator)
  alerts_sent:  [int]                       # thresholds already DM'd this cycle, subset of [50,25,10,0]
  updated_at

firms/{teamId}/creditLedger/{entryId}       # append-only audit; balance reconstructable
  type:         "topup" | "deduction"
  amount:       +N | -N
  balance_after: int
  reason / note / ref?
  channel_id?, client_name?, file_id?, doc_kind?, units?
  uploaded_by?                              # teammate who dropped (audit; owner is still charged)
  at
```

**Dropped from the spec:** `install_invites/`, `firms/{U}/creditBuckets/` (FIFO),
installer-`U…` keying, `pending_credits`/`active` status machine. Granted credits are
flat integers and never expire (an optional `expires_at` may live on a trial grant).

Every mutation (top-up **and** deduction) writes one ledger row and updates `balance`
+ alert state inside **one Firestore transaction**. (Single Cloud Run instance today;
the transaction is also the correct primitive if `max-instances` is ever raised — see
ADR-0016 amendment 2026-06-18.)

---

## Build slices

### Slice 1 — credit service + admin CLI  *(unblocks granting today)*
- `app/credit_service.py`: `read_balance`, `grant`, `deduct`, `ensure_firm`,
  `month_usage_by_channel` — all transactional; `client=` injection seam for hermetic tests.
- `accounting_agents/admin.py`: `grant`, `list-firms` (reads `workspaces/*` left-join `firms/*`,
  resolves names live via `users.info` when a firm doc is missing).
- Tests: `tests/test_credit_service.py`.
- **After this slice you can grant the trial firm and `list-firms`.**

### Slice 2 — gate + page-count capture
- Page count (`pypdf`) captured at file-event entry; carried for both gate and bank charge.
- Gate in the entry path: `balance ≤ 0` refuse; `page_count > balance` refuse (insufficient card).
- Tests: `tests/test_credit_probe.py` (or fold into `test_slack_runner.py`).

### Slice 3 — deduction on delivery
- Hook in `persist_and_deliver()` after `append_rows()`; batch path in `_flush_deferred_ledger_writes`.
- Charge `appended` (non-bank) or page count (bank); `file_id`-idempotent; replace/re-extract = 0.
- Tests: `tests/test_billing.py`.

### Slice 4 — visibility Tier 1 + alerts
- Delivery footer; `/ledgr credits` (add `credits` to `app/commands.py` parser + a Block Kit card).
- Low-balance DM at 50/25/10/0% of `cycle_start`, debounced; operator-channel mirror at 10%/0%.
- Tests: `tests/test_app_commands.py`, `tests/test_credit_alerts.py`.

### Slice 5 — App Home
- Manifest: `features.app_home.home_tab_enabled = true`; add `app_home_opened` bot event (all 5 manifests).
- `app/app_home.py`: build + `views.publish(user_id=<opener>)`; refresh on `app_home_opened` + after charge/grant.

---

## Operator runbook

```bash
gcloud auth application-default login            # one-time, as admin@qbsaiautomation.com
cd ~/Projects/Ledgr-QBS

# See every install + balance (prod = unprefixed namespace)
LEDGR_FIRESTORE_NAMESPACE= uv run python -m accounting_agents.admin list-firms

# Grant a trial (creates the firm record on the fly)
LEDGR_FIRESTORE_NAMESPACE= uv run python -m accounting_agents.admin grant --firm <team_id> --amount 500 --note "trial"
```

---

## File touch-list

**New:** `app/credit_service.py`, `app/app_home.py`, `accounting_agents/admin.py`,
`tests/test_credit_service.py`, `tests/test_billing.py`, `tests/test_credit_probe.py`,
`tests/test_credit_alerts.py`.

**Modify:** `accounting_agents/slack_runner.py` (gate + deduction hooks),
`app/commands.py` (`credits` subcommand), `app/blocks.py` (footer + cards),
`slack/manifest-*.json` (Home tab + `app_home_opened`; `users:read.email` for new installs),
`docs/qa/credit-system-live-qa-checklist.md`.

---

## Open / deferred

- **Trial amount for the first production firm** — business call (rec ~500). No code impact.
- Pushed operator low-balance **digest** (beyond the 10%/0% mirror) — post-MVP; `list-firms` covers it.
- Chat `get_credit_summary()` tool, Stripe self-serve, usage CSV export — out of scope (per ADR-0016).

---

## Verification gate (before any production grant)

```bash
uv run pytest tests/test_credit_service.py tests/test_billing.py tests/test_credit_probe.py \
              tests/test_credit_alerts.py tests/test_app_commands.py -q
```
Then QA-A→H on dev (QBS-AI / David-Workspace). Green dev run is the gate for the first prod grant.
