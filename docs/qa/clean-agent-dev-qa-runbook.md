# Clean Agent Dev QA Runbook

## What this is

A step-by-step guide for **you** (or QA) to test the **clean agent** path in a dev Slack workspace **after D.2 credits are wired**. Think of it like a checklist before flipping the big switch from the old bot brain to the new one.

The clean agent runs when this flag is on:

```bash
export LEDGR_USE_CLEAN_AGENT=1
```

Without that flag, Slack still uses the legacy graph — these steps won't exercise the new path.

---

## Recommended test order

Do these in order — each step builds on the last:

| Phase | Where | What you prove | Credits? |
|-------|--------|----------------|----------|
| **1** | `uv run pytest …` | Gate, delivery deduct, HITL wiring (hermetic) | Simulated in tests |
| **2** | `adk web ledgr_agent` or agents-cli | Brain + tool + extraction (real Gemini) | Playground = not billable |
| **3** | Slack dev bot + `LEDGR_USE_CLEAN_AGENT=1` | Full cutover: gate, delivery footer, dedup | Real (in-memory dev store) |

**Start with phase 2 (playground)** if you want to see the agent think and extract without Slack setup.
**Phase 3 (Slack)** is required to validate D.2 credits end-to-end.

---

## Before you start (5-minute setup)

1. **Pull latest code** on `main` (or your feature branch with D.2–D.4 merged).

2. **Run unit tests locally** (should be green):

   ```bash
   uv run pytest tests/ledgr_agent tests/test_clean_agent_slack_dispatch.py \
     tests/test_credit_delivery.py tests/test_durable_credit_wiring.py -q
   ```

3. **Set environment variables** for the dev bot:

   | Variable | Value |
   |----------|--------|
   | `LEDGR_USE_CLEAN_AGENT` | `1` |
   | `SLACK_BOT_TOKEN` | your dev bot token |
   | `SLACK_APP_TOKEN` | socket mode token |
   | `GOOGLE_API_KEY` | Gemini key for extraction |

   Leave `LEDGR_CHARGE_CREDITS_IN_TOOL` **unset** (or `0`). Credits should deduct on **Slack delivery**, not inside the tool.

4. **Restart the bot** from HEAD. A long-running process keeps old code in memory — restart is required after every code pull.

   ```bash
   uv run python slack_bot.py
   ```

   Socket mode is the recommended dev launcher. `uvicorn app.main:app` serves HTTP
   mode (Cloud Run parity) but does not use socket mode unless configured separately.

5. **Grant test credits** inside the **bot process** (not a separate terminal).

   The admin CLI uses its own memory — credits vanish when that command exits.
   For dev, set this on the **same env as the bot**:

   ```bash
   export LEDGR_DEV_CREDIT_GRANTS=T0YOURTEAMID:50
   ```

   Replace `T0YOURTEAMID` with your Slack workspace team id (`T…`). The bot
   applies the grant at startup via ``wire_shared_credit_service()``.

   Optional sanity check (separate process — balance here is **not** what the bot sees):

   ```bash
   uv run python -m accounting_agents.admin grant T0YOURTEAMID 50 --note "dev QA"
   uv run python -m accounting_agents.admin list
   ```

6. **Confirm client channel is onboarded** — run `/ledgr settings` in the channel and pick software + FY. Without a profile, the bot refuses the drop.

---

## Quick map: what each check proves

| # | Plain English | Clean-agent path |
|---|---------------|------------------|
| 1 | Happy invoice → ledger, no pause | Tool runs → maps to ledger → delivers |
| 2–3 | Low-confidence → one review card → Approve delivers | HITL via Firestore interrupt (`kind=clean_agent_batch`) |
| 4 | Chat edit asks before changing | Chat lane (unchanged by clean agent) |
| 5 | Right Excel columns per ERP | Exporter from client profile |
| 6 | Zero credits → blocked **before** AI | Slack gate in `process_file_event` |
| 7 | Same file twice → no second charge | Dedup at ledger + idempotent deduct key |

Full detail for checks 1–5 lives in [clean-agent-cutover-checklist.md](./clean-agent-cutover-checklist.md). This runbook adds **credit-specific steps** for 6–7 on the flag-on path.

---

## Check 6 — Zero credits (live)

**Goal:** Bot says "out of credits" and never calls Gemini.

1. Set balance to zero:

   ```bash
   # Grant 0 won't work — use a fresh team id or manually zero in dev store.
   # Easiest: use admin list, then test on a workspace with no grant yet.
   python -m accounting_agents.admin list
   ```

   For dev with in-memory store, restart bot **without** granting credits, or use a dedicated test team id `T-ZERO` that you never grant.

2. Ensure the client profile has `firm_id` or `slack_team_id` = that team id (Firestore `clients/{id}`).

3. Drop a PDF invoice (synthetic number **INV-005**).

**Expect in Slack:**

- Status: ❌ Out of credits
- Message: "You're out of credits…"
- No xlsx attachment, no delivery card

**Expect in logs/trace:**

- `process_document_batch` **not** called (blocked at Slack gate), OR tool returns `status=blocked` if gate only in tool
- `llm_call_count` = 0

**Pass:** No ledger write, no AI spend.

---

## Check 7 — Dedup, no double charge (live)

**Goal:** Dropping the same invoice twice only charges once.

1. Grant credits: `python -m accounting_agents.admin grant T… 20`

2. Drop invoice **INV-006** → wait for delivery card.

3. Note footer: `Used 1 credit · N remaining`.

4. Re-drop the **same** PDF (or same invoice number).

**Expect:**

- Second time: "Already recorded" / duplicate path
- Balance unchanged after second drop
- No second "Used 1 credit" footer

**Pass:** `admin list` shows one credit spent, not two.

---

## Check 1 — Smoke after credits (recommended order)

Run check **1** (normal invoice INV-001) **after** grants are in place:

- Delivery card + xlsx
- Footer: `Used 1 credit · … remaining`
- No HITL card for a clean invoice

---

## HITL + credits (checks 2–3)

1. Drop low-confidence invoice **INV-002** → grouped review card.
2. Click **Approve**.
3. Delivery card appears; **one** credit deducted (idempotency key `{channel}:{file_id}:deliver`).
4. Reject path: **no** credit charge.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Drops always process with 0 credits | `firm_id` missing on client profile | Set `firm_id` or `slack_team_id` = workspace team id in Firestore |
| Admin grant doesn't affect bot | Bot not restarted; separate in-memory store | Restart bot; use same process for admin + bot in dev |
| Still uses old graph | Flag off | `export LEDGR_USE_CLEAN_AGENT=1` and restart |
| Double charge | `LEDGR_CHARGE_CREDITS_IN_TOOL=1` | Unset that env var |
| Gate never blocks | No firm id on profile | Gate fail-opens when firm is unknown (by design for playground) |

---

## Sign-off

| Field | Value |
|-------|--------|
| Tester | |
| Date | |
| Branch / commit | |
| Workspace team id | |
| Checks passed | 1 ☐ 2 ☐ 3 ☐ 4 ☐ 5 ☐ 6 ☐ 7 ☐ |
| Credits footer seen | ☐ |
| Ready for D.6 flag default flip | ☐ |

---

## Related docs

- [clean-agent-cutover-checklist.md](./clean-agent-cutover-checklist.md) — full seven checks
- [credit-system-live-qa-checklist.md](./credit-system-live-qa-checklist.md) — production credit slices
- Plan: `docs/superpowers/plans/2026-06-24-clean-agent-finish-and-cutover.md` (D.2, D.6)
