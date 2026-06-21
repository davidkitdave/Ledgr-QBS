# Ledgr-QBS — Slack Credit System (design spec)

**Date:** 2026-06-17  
**Status:** Approved (planning). Ready for slice-by-slice implementation and QA verification.  
**Superseded by:** The billing ADR is **[ADR-0016](../../adr/0016-credit-deduction-and-manual-topup.md)** (NOT "ADR-0013" — that number is the ADK-adoption matrix; this reference was always stale). Where this spec and ADR-0016 conflict, **ADR-0016 governs.** The execution-ready doc is the plan: **[plans/2026-06-20-slack-credit-system.md](../plans/2026-06-20-slack-credit-system.md)**. Read the Reconciliation banner below before implementing anything here.  
**Reference implementation plan:** Cursor plan *Slack Credit System* (conversation 2026-06-16/17).  
**Live QA checklist:** [docs/qa/credit-system-live-qa-checklist.md](../../qa/credit-system-live-qa-checklist.md)

## Context

Ledgr-QBS is a Slack-native document pipeline (one channel = one client). Customers are **accounting firms** who install the Ledgr Slack app, onboard client channels, and drop documents for extraction and ledger delivery.

Credits are **prepaid units** (see [CONTEXT.md](../../../CONTEXT.md)): 1 credit = 1 billable unit. Payment is **out-of-band** (invoice → operator grant). The Ledgr web app (`Ledgr` repo) is **reference only** for the FIFO Firestore credit pattern — QBS implements its own ledger in project `ledgr-qbs`.

This spec defines hierarchy, Firestore schema, install→grant→use flows, probe/charge billing, Slack UX for credit visibility, operator tooling, and **verification criteria** for QA sign-off.

> ## ⚠️ Reconciliation (2026-06-20) — read first
> A `/grill-with-docs` session reconciled this spec with **[ADR-0016](../../adr/0016-credit-deduction-and-manual-topup.md)**, which **governs**, and produced the execution-ready **[plan](../plans/2026-06-20-slack-credit-system.md)**. The following parts of this spec are **superseded**:
>
> - **Firm = installer user `U…`** → **Firm = workspace `team_id`** (installer is identity metadata only). *Decision #1 below is reversed; the §52 "Amends CONTEXT.md" note is void — CONTEXT already says workspace = firm.*
> - **FIFO credit buckets + `expires_at` automation** → a **single flat integer balance**, no buckets, no expiry. `creditBuckets` is dropped.
> - **Two-phase Gemini-segmentation probe** (Decision #4) → a free **`balance ≤ 0` + page-count ceiling** gate; charge still on delivery.
> - **Auto-grant on install via `?ref=` invites** (Decisions #6, #7) → **manual `grant` only**; a trial is a grant with `--note "trial"`. `install_invites` is dropped.
> - **Absolute alert thresholds (≤50/≤10/0)** → **percentage of last top-up** (50/25/10/0%), debounced, with operator-channel mirror at 10% and 0%.
> - **Three operator scripts** → one `accounting_agents.admin` CLI (`grant`, `list-firms`).
> - **`ensure_firm_on_install` / OAuth-success enrichment** → **lazy `ensure_firm`** the `grant` CLI triggers; **no backfill script**. (Verified live: `users.info` resolves installer names on existing installs with the current `users:read` scope — no reinstall.)
>
> Everything else (billable-unit definitions, charge-on-delivery, idempotency, the QA matrix, App Home as MVP) stands.

---

## Goals

1. **Install → account:** OAuth install creates a Firm (installer Slack user) and links workspace → firm.
2. **Grant:** Operator can pre-provision credits (Cast Unity) or grant after payment (pay-first).
3. **Gate:** Lightweight credit probe before expensive pipeline; block when balance insufficient.
4. **Charge:** Deduct credits only when documents are **written to the ledger** (delivery), not on chat or failed/rejected/deduped paths.
5. **Visibility:** Passive job footers, `/ledgr credits`, and **App Home** dashboard (per-firm personalized).
6. **Audit:** FIFO buckets + ledger in Firestore; usage by channel for reporting.

## Non-goals (MVP)

- Stripe / in-Slack payment
- Credit rollover caps, expiry automation (except optional `expires_at` on grant)
- Per-client credit balances (credits are **firm-wide**, shared across channels)
- Chat assistant credit tool
- Charging Gemini token usage to customers (internal cost only)

---

## Decisions locked

| # | Decision |
|---|----------|
| 1 | ~~Firm = Slack user who installed (`U…`)~~ → **REVERSED (ADR-0016): Firm = workspace `team_id` (`T…`)**; installer is identity metadata only |
| 2 | **One credit balance per Firm**, shared across all client channels in workspace(s) |
| 3 | **Charge account owner** on teammate uploads (`team_id → workspace.firm_id`, not `message.user`) |
| 4 | **Two-phase billing:** probe quote (gate) → delivery charge (authoritative) |
| 5 | **Billable unit** matches CONTEXT: invoice/receipt = 1 per unique doc written; bank = 1 per page written; dedup/reject = 0 |
| 6 | **Install link for paying customers:** `{SLACK_BASE_URL}/slack/install?ref={INVITE}` (not Slack Manage Distribution URL alone) |
| 7 | **Pre-provisioned invite** auto-grants on install (Cast Unity); pay-first stays `pending_credits` until operator grant |
| 8 | **App Home** is MVP for credit dashboard (not Phase 2) |
| 9 | **Users may install before credit code ships** — backfill firm + grant later; no reinstall required |

**Amends CONTEXT.md:** Firm glossary entry must state installer-user identity and shared balance (not “workspace = firm”).

---

## 1. Entity hierarchy

```
Firm (installer Uxxx)
  └── Credit balance (ONE pool)
        ├── #client-a (Client channel — COA/profile only, NO credit scope)
        ├── #client-b
        └── #client-c

Workspace (Txxx) — OAuth container
  └── workspaces/{key}.firm_id → Uxxx
```

| Entity | ID | Credits? | COA? |
|--------|-----|----------|------|
| Firm | Installer `U…` | Yes | No |
| Workspace | `T…` | No | No |
| Client | Channel `C…` | No | Yes |

---

## 2. Firestore schema (`ledgr-qbs`)

Namespace: optional `LEDGR_FIRESTORE_NAMESPACE` prefix via `_ns()` in `accounting_agents/config.py`.

### Existing (today)

| Collection | Purpose |
|------------|---------|
| `clients/{id}` | Profile, COA, entity_memory, processing_log |
| `channels/{id}` | channel → client_id |
| `workspaces/{key}` | OAuth install (bot token, team_id, user_id) |
| `oauth_states/{state}` | OAuth CSRF |
| `interrupts/`, `processed/` | HITL, dedup |

### New (credit system)

```
install_invites/{ref}
  label, credits_on_install, source, expires_at?, status: open|claimed, firm_id?

firms/{firmId}                    # firmId = installer user_id U…
  slack_user_id, display_name, primary_team_id, team_ids[]
  invoice_ref, status: pending_credits|active
  installed_at, activation_requested_at?

firms/{U}/credits/balance
  total_credits, updated_at

firms/{U}/creditBuckets/{id}
  credits_granted, credits_remaining, source, granted_at, expires_at?, note?

firms/{U}/creditLedger/{id}
  state: granted|committed|…
  reference_id, credits_used, bucket_allocations
  team_id, channel_id, client_id, client_name, filename
  probe_estimate, balance_after, uploaded_by, created_at

workspaces/{key}
  …existing…
  firm_id, installer_user_id, installed_at

clients/{channelId}
  …existing…
  firm_id   # denormalized audit only
```

---

## 3. Install & operator flows

### 3.1 Install links

| Link | Source | Credits on install |
|------|--------|-------------------|
| **Recommended** | `https://ledgr-640071771526.asia-southeast1.run.app/slack/install?ref=CAST-UNITY-2026` | Auto-grant if invite exists |
| Slack Shareable URL | api.slack.com → Manage Distribution | Manual grant only |
| No ref | `…/slack/install` | `pending_credits` |

Production base URL: Cloud Run `SLACK_BASE_URL`. Redirect: `…/slack/oauth_redirect`.

### 3.2 Mode A — Pre-provisioned (Cast Unity)

1. Operator: `create_install_invite.py --ref CAST-UNITY-2026 --credits 60000 --label "Cast Unity"`
2. Send link with `?ref=CAST-UNITY-2026`
3. Customer OAuth Allow → `ensure_firm_on_install` → auto-grant → welcome DM → operator notify
4. Verify: `list_firms.py --ref CAST-UNITY-2026` or Firestore `firms/{U}/credits/balance`

### 3.3 Mode B — Pay-first

1. Send link with `?ref=INV-2026-042`
2. Install → `pending_credits` → Request activation button → operator channel
3. After payment: `grant_credits.py --ref INV-2026-042 --amount 500`

### 3.4 Install now, credits later (backward compatible)

If customer installs **before** credit code ships:

- `workspaces/{key}` is created with `user_id`, `team_id`
- No gating until code deploys
- Later: backfill `firms/{user_id}` from workspace docs + `grant_credits.py --user U…`
- **No reinstall required**

`?ref=` is ignored until OAuth state + invite handling ships.

### 3.5 Operator scripts (MVP)

| Script | Purpose |
|--------|---------|
| `scripts/create_install_invite.py` | Pre-provision ref + credits |
| `scripts/grant_credits.py` | Grant by `--user`, `--ref`, or after `--search` |
| `scripts/list_firms.py` | List pending/active, filter by ref |

Env: `LEDGR_OPERATOR_CHANNEL_ID` for install/activation notifications.

---

## 4. Billing rules

### 4.1 Probe (before pipeline)

Order: `profile check → download → validate → CREDIT PROBE → balance gate → ADK pipeline`

| Doc type | Estimate method |
|----------|-----------------|
| Bank (simple) | `pypdf` page count |
| Invoice/receipt/mixed | Gemini Flash Lite segmentation (port from Ledgr `segmentation.py`) |
| Fallback | Conservative floor; never under-count billable units |

- Batch: sum estimates; gate on total before any file enters pipeline
- Probe cost **not** charged to customer

### 4.2 Charge (on delivery)

Hook: `persist_and_deliver` after successful `append_rows`, when `append_result["appended"] > 0`.

| Case | Credits |
|------|---------|
| New invoice/receipt delivered | Count of newly appended batches |
| Bank delivered | Billable pages written |
| Dedup only | 0 |
| Re-extract replace-in-place | 0 |
| Reject / unreadable | 0 |

Idempotency key: `{channel_id}:{file_id}:deliver`.

Quote vs charge: actual ≤ estimate normally; dedup charges less; rare underestimate charges actual and logs probe miss.

---

## 5. Slack UX — credit visibility

Three levels; all read from `creditLedger` + `creditBuckets`.

### 5.1 Passive (client channels)

| Moment | Copy pattern |
|--------|--------------|
| Before job | `Up to N credits · X remaining` |
| After delivery | `Used N credits · X remaining` |
| Batch complete | `This job: N credits · Account: X remaining` |
| Insufficient | Block + contact top-up |
| Dedup | `No credits used — already in ledger` |

### 5.2 On-demand — `/ledgr credits`

- **Ephemeral** — only requester sees it
- **Master view** when run outside client channel
- **Channel view** when run inside `#client-name` (this channel usage + account remaining)
- Add to manifest `usage_hint` and `/ledgr help`

### 5.3 Persistent — App Home (MVP)

- Sidebar → Ledgr → **Home** tab (Messages tab optional/off)
- `views.publish(user_id=installer)` — **each firm sees their own balance**
- Blocks: header (remaining), context (used since top-up), usage by client channel, recent activity, actions (Refresh, Request top-up, Full history modal)
- Refresh on: `app_home_opened`, after charge/grant (best-effort)

**Slack config:** Enable Home tab; add `app_home_opened` to manifest bot events.

### 5.4 Alerts (post-MVP)

DM installer at ≤50, ≤10, 0 credits (`last_alert_at` debounce).

---

## 6. Implementation slices

| Slice | Deliverable | Verification section |
|-------|-------------|-------------------|
| 1 | ADR-0013, credit_service, firm_service, scripts, docs | — |
| 2 | OAuth wiring, ensure_firm_on_install, invites, activation UX | QA-A, QA-B |
| 3 | credit_probe.py | QA-C (partial) |
| 4 | Gate + billing.py + persist_and_deliver charge | QA-C, QA-D, QA-E |
| 5 | credits_report, app_home, /ledgr credits, footers | QA-F |
| 6 | Low-balance DMs | — |

---

## 7. Verification matrix (requirements → how to verify)

Use this table in review sessions. Mark Pass/Fail in [credit-system-live-qa-checklist.md](../../qa/credit-system-live-qa-checklist.md).

| ID | Requirement | Automated test | Live QA |
|----|-------------|----------------|---------|
| R1 | OAuth install creates `firms/{U}` | `test_firm_service` | QA-A3 |
| R2 | `?ref=` auto-grants from invite | `test_activation_flow` | QA-A4, QA-G3 |
| R3 | Workspace links `firm_id` | `test_firm_service` | QA-A5 |
| R4 | Pay-first pending + Request activation | `test_activation_flow` | QA-B |
| R5 | Probe blocks when balance low | `test_slack_runner` | QA-C2, C7 |
| R6 | Probe quotes before pipeline | `test_credit_probe` | QA-C4–C6 |
| R7 | Charge on delivery only | `test_billing` | QA-D1–D5 |
| R8 | Dedup = 0 charge | `test_billing` | QA-D3 |
| R9 | Idempotent charge | `test_credit_service` | QA-D6 |
| R10 | Teammate drop → owner charged | `test_firm_service` | QA-E |
| R11 | App Home shows balance | — | QA-F1 |
| R12 | `/ledgr credits` ephemeral master/channel | `test_app_commands` | QA-F3–F4 |
| R13 | Ledger has channel + balance_after | `test_credits_report` | QA-D2 |
| R14 | Cast Unity 60k end-to-end | — | QA-G |
| R15 | Backfill works for pre-install users | manual script | §3.4 manual |

**Automated gate (before live QA):**

```bash
pytest tests/test_credit_service.py tests/test_billing.py tests/test_credit_probe.py \
       tests/test_firm_service.py tests/test_credits_report.py tests/test_activation_flow.py \
       tests/test_app_commands.py -q
```

---

## 8. Key files (implementation touch list)

**New:** `app/credit_service.py`, `app/firm_service.py`, `app/credit_probe.py`, `app/billing.py`, `app/credits_report.py`, `app/app_home.py`, `app/activation.py`, `scripts/grant_credits.py`, `scripts/list_firms.py`, `scripts/create_install_invite.py`, `docs/adr/0013-*.md`, `docs/credits-and-billing.md`

**Modify:** `accounting_agents/slack_runner.py`, `app/blocks.py`, `app/commands.py`, `app/installation_store.py`, `app/slack_app.py`, `slack/manifest-*.json`, `CONTEXT.md`, `docs/slack-setup.md`

**Reference (read-only):** Ledgr `firestore_credit_service.py`, `segmentation.py`, ADR-028

---

## 9. Open / deferred

- Chat `get_credit_summary()` tool in assistant
- Stripe self-serve top-up
- Monthly rollover / FIFO expiry automation
- Export usage CSV for operator billing reconciliation

---

## 10. Sign-off

| Role | Name | Date | Slice verified |
|------|------|------|----------------|
| Operator (grant flow) | | | 2 |
| Engineering (automated) | | | 1–5 |
| Live QA (Slack + Firestore) | | | A–G |
| Cast Unity dry run | | | G |

**Ready for production customer when:** QA-G pass + R1–R14 verified + ADR-0013 merged.
