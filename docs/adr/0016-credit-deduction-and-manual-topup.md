# 0016 — Credit deduction and manual top-up (proposed)

- **Status:** Proposed — **not implemented**
- **Date:** 2026-06-17
- **Deciders:** Ledgr team

## Amendment — Credit deduction read point + multi-instance OCC note (2026-06-18)

**Deduction read point:** credit deduction must read the `appended` count from
the **flush result** returned by `_flush_deferred_ledger_writes`, not from the
per-doc deferred payload. Per-doc deferred payloads carry `appended=0` at
collection time (the write has not yet happened); the actual written row count
is only known after the batch-reduce flush completes. Deducting from the per-doc
result would charge 0 credits per document regardless of what was written.

Concretely: in `persist_and_deliver()`, deduct after `append_rows()` returns
using its `appended` field. In the batch path, deduct inside
`_flush_deferred_ledger_writes` after each group write, using the `appended`
count from that group's flush result.

**Multi-instance future:** the current deployment is `min-instances=1,
max-instances=1`, making the in-process credit counter and `threading.Lock`
sufficient. If `max-instances` is ever raised above 1, the in-process lock
and counter are insufficient — two Cloud Run instances can read the same
balance concurrently and both deduct, producing a double-spend. For the
multi-instance case, credit deduction must use a **Firestore transaction
or etag-based optimistic concurrency control (OCC)** on the
`firms/{firmId}/credits` document. This is not required at current scale
but must be the first change made before raising `max-instances`.

## Context

Ledgr needs to meter usage and charge firms for the work it does. The
[[Firm]] is the paying customer; processing a document consumes credits;
the balance is held **per Firm** (one balance across all of that firm's
Client channels). The domain language is already settled in `CONTEXT.md`
([[Credit]], [[Billable unit]], [[Top-up]]) — this ADR records the
**mechanism**, not the vocabulary.

We studied a sibling product (`/Users/davidkitdave/Documents/GitHub/Ledgr`,
a Next.js + Stripe web app) only as a **conceptual reference**. We adopt its
*model* (per-firm balance, billable unit, an append-only credit ledger,
double-charge guards) and reject its *stack* (Stripe checkout/webhooks,
credit buckets with FIFO expiry, a React billing dashboard, a self-serve
trial funnel). Ledgr-QBS is Slack + ADK + Firestore, and payment is handled
**out-of-band** (the firm pays the developer; there is no payment processor
in the loop).

Today **no billing infrastructure exists** in this repo. The document
pipeline already exposes the facts we need at the right moment:
`persist_and_deliver()` in `accounting_agents/slack_runner.py` knows, after
`SlackLedgerStore.append_rows()` returns, how many units were **`appended`**
(newly written to the ledger) vs **`deduped`** (duplicates of a document
already present). Firestore already backs sessions, client profiles, and
HITL interrupts, so it is the natural home for credit state too.

## Decision

### 1. Storage — one firm doc + a balance doc + an append-only ledger (no buckets, no expiry)

```
firms/{firmId}                          → { team_id, team_name,
                                            installer_user_id, installer_name,
                                            installer_email?, installed_at }
firms/{firmId}/credits                  → { balance: int, updated_at }
firms/{firmId}/creditLedger/{entryId}   → { type: "topup" | "deduction",
                                            amount: +N | -N,
                                            balance_after: int,
                                            reason, channel_id?, file_id?,
                                            doc_kind?, units?, ref?, at }
```

`firms/{firmId}` is the **canonical firm record** — it holds both *identity*
(so the developer can find a firm by a human name/email, see §7) and the
*billing* state below it. `firmId` is the Slack workspace `team_id`.

**Billing state is Firestore-only.** Credits are integers and audit rows —
no documents, no blobs — so billing touches neither GCS nor the artifact
service. This is consistent with ADR-0002 (Slack is the system of record;
GCS was retired): we hold structured metadata, not customer files. The
customer's PDFs and workbooks live in their own Slack workspace; the credit
system never reads or stores them.

`balance` is the live cached number; `creditLedger` is the full audit trail
from which the balance can always be reconstructed. There are **no credit
buckets, no FIFO, and no expiry** — granted credits stay until spent. Every
mutation (top-up *and* deduction) writes one ledger row and updates `balance`
inside a **single Firestore transaction**, so concurrent deliveries cannot
double-spend. `firmId` is the Slack workspace identity (`team_id`), matching
how installs and client profiles are already keyed.

### 2. Deduction model — gate at upload, deduct on delivery

No reserve/commit/release. Instead:

- **Gate at upload.** When a file lands, if the firm's `balance <= 0`, refuse
  the job up front with a "top up to continue" message. The document is never
  processed.
- **Deduct on delivery.** A document is billable **only when written to the
  ledger** (per [[Billable unit]]). After `append_rows()` succeeds, deduct the
  `appended` count; **never** charge `deduped` rows.
- **Mid-batch overrun.** If a firm has *some* balance but a batch exceeds it,
  process documents until `balance` hits zero, then skip the remainder and
  report which docs were delivered vs skipped for lack of credit.

### 3. Billable unit per document kind

- **Invoice / receipt:** 1 credit per **unique document written** (the
  `appended` count). Deduped duplicates and skipped SOA cover pages are free.
- **Bank statement:** 1 credit per **source-PDF page**. This requires
  capturing the source PDF's page count at upload and carrying it through to
  deduction — the pipeline today works in transaction rows and
  currency/account batches, which do **not** correspond to pages.

### 4. Double-charge & refund guards

- Deduct on the `appended` count only → re-extraction (ADR-0010) of a document
  already in the ledger dedups to `appended=0` and is **not** re-charged.
- An **idempotency key on `file_id`** in `creditLedger` prevents a
  double-click or job retry from deducting twice even if it writes.
- **Replace** (`replace=True`) of an already-charged `doc_key` is **free** —
  the firm already paid for that document identity.
- **No refund on delete.** A credit represents work performed, not a row that
  currently exists; deleting a delivered document does not return credits.

### 5. Top-up — a developer-run admin CLI

Firms install Ledgr into **their own** Slack workspaces (Model B); the
developer is not a member of those workspaces and cannot run a slash command
there. The developer *does* hold Firestore credentials. Top-ups are therefore
a CLI the developer runs after raising a manual invoice:

```
python -m accounting_agents.admin grant \
    --firm T0123ABC --amount 500 --ref "INV-2026-014" --note "June top-up"
```

It increments `balance` and writes a `topup` ledger row (amount, invoice ref,
note, timestamp) in one transaction, then prints the new balance. Editing
Firestore by hand is disallowed — it skips the ledger row and breaks the audit
trail. New firms **start at 0**; a trial/demo is just a `grant` with
`--note "trial"`. No automatic install-time grant, no expiry timer.

### 6. Visibility surfaces

- **Delivery card** (`persist_and_deliver`): one line — `N credits used ·
  M remaining` — plus a low-balance nudge when `M` drops below a threshold
  (e.g. 10). Batch drops show one summary line at the end, not per-file.
- **App Home** (net-new `app_home_opened` handler — none exists today): the
  hero balance, the last top-up (amount + date), this-month usage **broken
  down per client channel** (cheap, since each `deduction` row carries
  `channel_id` and `doc_kind`), and an out-of-band "need more credits?"
  contact footer (no payment button).

### 7. Knowing *which* firm to grant — identity capture at install

The `grant` CLI takes a `--firm <team_id>`, but `team_id` (`T0123ABC…`) is
opaque, and Slack's OAuth `Installation` payload carries **no person name and
no email** — only `team_name`, `enterprise_name`, and an opaque installer
`user_id`. So nothing today lets the developer go from "the firm that paid
invoice INV-014" to the right `team_id`.

We close the loop by **enriching the `firms/{firmId}` doc at install**:

- **Name (free, no scope change).** The bot already holds the `users:read`
  scope. At the OAuth-success callback, call `users.info(installer_user_id)`
  and store `installer_name` (`profile.real_name` / `display_name`). Firestore
  is then searchable by a real person's name and by `team_name`.
- **Email (requires a scope change + reinstall).** Capturing `installer_email`
  needs the **`users:read.email`** scope added to `BOT_SCOPES` and the Slack
  manifests; every firm must re-authorize once before the email becomes
  available via `users.info`. Treated as an optional enhancement, not a
  blocker — the name + workspace name are enough to identify a firm.

Discovery workflow: firm pays → developer runs **`admin list-firms`** (dumps
`team_id · team_name · installer_name · installer_email? · installed_at` from
the `firms` collection) → matches by name/email → `admin grant --firm
<team_id>`. The App Home (§6) also shows the firm its own `team_id` as a
"Ledgr account ID" so it can be quoted on payment correspondence, removing
the name-matching guesswork entirely.

## Why not the sibling product's mechanisms?

| Option | Why not |
|--------|---------|
| Stripe checkout + webhooks | Payment is out-of-band (manual invoice). No processor in the loop. |
| Credit buckets + FIFO expiry | Solves Stripe's monthly-grant rollover problem. We have manual, non-expiring grants — buckets would be dead complexity. |
| Reserve / commit / release | Needed when a job can silently fail after holding credits. "Deduct on delivery" + the `appended`/`deduped` distinction gives the same correctness with far less state. |
| React `/profile` billing dashboard | Wrong stack. The firm lives in Slack; App Home is the native surface. |
| Auto-granted trial (50cr / 14d) | A self-serve funnel artifact. We onboard manually; a trial is a manual grant. |
| Refund on delete | Lets a firm process-then-delete to claw back credits, and adds reversal edge cases. A credit = work done. |

## Trade-offs

- **No reservation** means a long batch can drain the balance to zero
  mid-run; mitigated by the gate + the per-doc "skipped for lack of credit"
  report rather than by holding credits up front.
- **Per-page bank charging** needs new plumbing (page count at upload). The
  alternative units (per account/currency section, per whole statement) reuse
  existing data but mis-price long scanned statements, which are the expensive
  ones to extract.
- **Balance write must not block delivery.** If the deduction transaction
  fails *after* a successful delivery, the firm still gets their result; the
  failure is logged and retried out-of-band. We never punish a firm for our
  billing-write infrastructure.

## Implementation order (when this ADR is approved)

1. Firestore credit service (`balance` + `creditLedger`, transactional
   `grant`, `deduct`, `read_balance`, `month_usage_by_channel`).
1a. Firm-identity capture: at OAuth-success, `users.info(installer)` →
   write `firms/{firmId}` with `team_name` + `installer_name` (+
   `installer_email` once `users:read.email` is added and firms reinstall).
2. Admin CLI: `grant` (top-up) **and** `list-firms` (find the `team_id` by
   `team_name`/`installer_name`/`installer_email`).
3. Upload gate in the file-event entry path.
4. Source-PDF page-count capture at upload; thread it to deduction.
5. Deduction hook in `persist_and_deliver()` after `append_rows()`
   (appended-only, `file_id`-idempotent), plus the batch-flush path.
6. Delivery-card credits line + low-balance nudge.
7. App Home (`app_home_opened`) view: balance + per-client month usage +
   last top-up + contact footer.
8. Live QA on a dev firm: grant → upload-gate at 0 → process → verify
   delivery line, App Home, and ledger audit rows.
```

## Amendment — Grilling resolutions (2026-06-20)

A `/grill-with-docs` session resolved the open questions and the conflict between this
ADR and `docs/superpowers/specs/2026-06-17-slack-credit-system-design.md`. **This ADR
governs; that spec is superseded on every point below.** The reconciled, execution-ready
plan lives at `docs/superpowers/plans/2026-06-20-slack-credit-system.md`.

1. **Firm = workspace `team_id`, confirmed.** The spec's "Firm = installer user `U…`" is
   rejected: a workspace is permanent, an installer can leave or a different admin can
   reinstall, and per-installer keying would split one firm's balance. The installer is
   captured as **identity metadata only** (`installer_user_id`, `installer_name`), never as
   the key. *Verified live 2026-06-20:* `users.info` on the stored per-workspace bot token
   resolves real installer names for all existing installs — no reinstall, with the
   `users:read` scope already held.

2. **No firm-record bootstrap hook; `ensure_firm(team_id)` is lazy + idempotent.** The
   `grant` CLI calls it, so granting an already-installed firm creates its record on demand
   from the `workspaces/{key}` doc. This replaces §7's "enrich at OAuth-success callback"
   and removes any backfill script — `list-firms` reads `workspaces/*` (the real install
   list) left-joined to `firms/*` for balance.

3. **Email is forward-only.** `users:read.email` is added to scopes for *new* installs;
   existing firms are **never** forced to reinstall. Name + workspace name + the
   account-ID-on-invoice convention identify a firm without it.

4. **Gate = `balance ≤ 0` refuse, then a free page-count ceiling** (`page_count > balance`
   → refuse). The spec's Gemini-Flash-Lite segmentation probe is dropped: the charge is on
   delivery anyway, so the gate only needs a deterministic upper bound, and the bank page
   count is reused as the bank billable unit (computed once).

5. **Bank charge = every source-PDF page** (not just extracted pages) — keeps the gate
   estimate and the final charge identical and trivially auditable.

6. **Percentage low-balance alerts (supersedes the absolute ≤50/≤10/0 in §6/the spec).**
   Denominator = `cycle_start` (balance immediately after the most recent top-up). DM the
   installer once each at **50 / 25 / 10 / 0%** of `cycle_start`, debounced via an
   `alerts_sent` set on the credits doc; a top-up resets `cycle_start` and clears
   `alerts_sent`. The **10% and 0%** alerts are **also mirrored to
   `LEDGR_OPERATOR_CHANNEL_ID`** so follow-up is pushed, not merely visible. Schema gains
   `firms/{teamId}/credits.cycle_start` and `.alerts_sent`.

7. **App Home is net-new** (no `app_home` block or `app_home_opened` event exists today;
   neither needs a new OAuth scope → no reinstall): hero balance + a 50/25/10 %-remaining
   bar + **per-client usage** (free from each deduction row's `channel_id`) + recent
   activity + the account ID + a Request-top-up action posting to the operator channel.

8. **No FIFO buckets, no auto-grant-on-install invites, one `accounting_agents.admin` CLI**
   (not the spec's three scripts) — `install_invites` and `creditBuckets` are dropped.
