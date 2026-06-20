# 0017 — Intelligent escalation: quality-gated open-set HITL + adaptive learning gate

- **Status:** Accepted
- **Date:** 2026-06-18
- **Deciders:** Ledgr team

## Context

A single expense-claim PDF forced a human-review pause. Root cause: `"expense_claim"`
was not in the Engine's recognised doc-type list, so it classified as `other`, and
`detect_struggle` in `accounting_agents/nodes.py` hard-tripped review whenever
`doc_type == "other"` — regardless of whether extraction was clean and the totals
reconciled. The system escalated on a **label**, not on a **quality signal**.

A real bookkeeper working a shoebox of mixed documents does the opposite: they never
fail closed on an unfamiliar layout, post from first principles, query the client
only on **material ambiguity** (won't reconcile, missing required substantiation,
brand-new vendor, illegible document), and over time learn the client so queries
decay. The newest AI bookkeeping products (e.g. Dext AI Assist, 2026) mirror this
exactly: categorise from supplier history, attach confidence + plain-language
reasoning to every entry, post the routine automatically, surface only the genuinely
ambiguous, and learn from each correction.

Confidence-score HITL is a recognised pattern — Document AI supports field-level
confidence thresholds that route to a human review queue. However, **raw
self-reported LLM confidence is nondeterministic and uncalibrated** per Google's
own guidance: a model may report 0.95 for a hallucinated field. Relying solely on
self-reported probability scores as HITL gates is therefore unsafe. The correct
approach is to pair **deterministic rule-based quality gates** (reconciliation,
required-field presence, structural bundle checks) with an **LLM-as-judge critic**
that clears ambiguous-but-not-broken signals.

This ADR records the design that replaces label-based escalation with
quality-based escalation, and introduces a per-client adaptive learning gate that
reduces escalation over time as the Engine gains familiarity. It builds on ADR-0003
(HITL mechanism), ADR-0004 (Corrections learning), ADR-0005 (canonical schema),
and ADR-0011 (Understand layer).

## Decision

### 1. Escalate on material ambiguity, not on doc-type label

`detect_struggle` triggers [[Review (HITL)]] only when **at least one hard signal
is present.** A clean, reconciled extraction with a novel doc-type label posts
without a pause. The two existing HITL gates (`detect_struggle` /
`review_extraction_node` for extraction quality, and `approval_gate` for posting
approval) are preserved and guard different axes — they are **not** consolidated.

### 2. Signal taxonomy — soft vs hard

**Soft signals** (may be cleared by the critic; never alone sufficient to escalate):

| Signal | Meaning |
|--------|---------|
| `doc_type_other` | Doc type resolved to `other` (unclassified but processable) |
| `doc_type_unfamiliar` | Doc type is recognised but has low prior seen count for this client |
| `low_classify_confidence` | Classifier self-reported confidence below threshold |
| `direction_uncertain` | Sales vs purchase direction ambiguous |

**Hard signals** (always escalate; critic cannot clear them):

| Signal | Meaning |
|--------|---------|
| `unreconciled` | Extracted total does not reconcile to source |
| `lines_empty` | No ledger lines were extracted |
| `missing_required` | A required field (completeness contract, ADR-0005) is blank |
| `bundle_empty` | Multi-doc bundle produced zero processable documents |
| `processable_false` | Engine determined the document cannot be meaningfully booked |

`processable=False` is the **new** hard escalation path for truly unbookable
documents. The `other` label alone no longer routes to escalation; the Engine
attempts the Understand layer and posts what it can.

### 3. Critic (LLM-as-judge) may clear soft-only signals

When every tripped signal is soft, the existing `_run_reviewer_loop`
(Generator–Critic pattern) is invoked. On `REVIEW_VERDICT_OK` the Engine falls
through to the confident post path with no human pause. Zero signals → no critic
call (zero extra LLM cost on the happy path).

When hard signals are present, the bounded reviewer loop still runs — solely to
attempt a deterministic auto-fix via the HINTS re-extraction path. After the loop
returns, `detect_struggle` is re-run on the (possibly updated) state. The critic's
`ok` verdict **cannot** clear a remaining hard signal: if `detect_struggle` still
trips with a hard signal after the loop, the document escalates to a human
regardless of what the critic said. Only if `detect_struggle` is now clean (or
soft-only) **and** the critic returned `ok` does the Engine proceed — meaning a
genuine re-extraction fix resolved the hard signal, not LLM say-so.

The reviewer LLM instruction is strengthened to return `ok` for soft-only concerns
when the extraction reconciles and required fields are present.

### 4. Confident-path note

A document that passes without a pause (all signals clear, or critic returns `ok`)
posts a concise plain-language note alongside the delivery card — e.g. *"Posted
this expense claim — 3 lines, reconciles to $240, coded to Travel."* Post-hoc edits
by the user still become [[Correction]]s via the existing `_persist_corrections`
path, so the no-pause promise does not sacrifice correctability.

### 5. Scope of the no-pause promise

Levers 1–3 (label quality, critic, familiarity) only affect the **first** gate
(`detect_struggle` / `review_extraction_node`). The **terminal** `approval_gate`
still pauses when: (a) the document is multi-entity, or (b) `_needs_review` trips
on `tax_flagged` / low `tax_confidence`. *"Uploading an expense claim → no pause"*
holds for **single-entity, reconciled, tax-confident** documents only. A
multi-invoice bundle or a tax-uncertain line still pauses at the terminal gate —
that is correct behaviour (material ambiguity at the posting axis), and out of
scope for this ADR.

### 6. Per-client familiarity gate (adaptive learning; extends ADR-0004)

A new Firestore subcollection `clients/{client_id}/familiarity/{key}` (where `key`
is `doc_type` or `doc_type:vendor`) holds `{seen_count, last_seen_at,
last_direction}`. This is the [[Familiarity]] store (see `CONTEXT.md`).

- **Increment on unedited approval** — `handle_approval_action` on
  `decision=="approve"` (the edit modal emits `decision=="edit"`, so the split is
  structurally safe) and the confident-path finaliser both increment `seen_count`.
- **Reset on post-hoc Correction** — if a [[Correction]] lands for a key that has
  Familiarity, that key's `seen_count` is reset to 0. A corrected shape is not yet
  trusted.
- **Decay in `detect_struggle`** — when all signals are soft, the gate applies
  **most-specific-key gating**: if a dominant vendor is identifiable from the
  normalised invoice (supplier name for purchases, customer name for sales), only
  the compound `doc_type:vendor` key is consulted; the bare `doc_type` key is
  ignored. If no vendor is identifiable, the bare `doc_type` key is used as
  fallback. Suppression fires when the selected key's `seen_count >= 2` (constant
  `FAMILIARITY_THRESHOLD`). This closes the "4c reset partial-defeat" vector: a
  correction zeroes the compound key; the surviving bare key cannot rescue
  suppression for that vendor, because vendor-identifiable docs never consult the
  bare key.

`familiarity` is explicitly **not** called "confirmation": `committed_confirmations`
already exists in `accounting_agents/slack_runner.py` as the ADK Tool-Confirmation
idempotency marker, an unrelated mechanism. Using the same term would create
ambiguity. See `CONTEXT.md § Familiarity`.

## Consequences

- Novel but clean documents (expense claims, unfamiliar layouts) no longer dead-end
  in a mandatory review pause.
- Escalation rate decreases per client over time as familiarity accumulates.
- Hard signals still guarantee a human sees genuinely ambiguous or broken
  extractions — the safety bar is unchanged.
- The two-gate structure (extraction quality gate + posting approval gate) is
  preserved; this ADR only widens the "confident" path at the first gate.
- A new `processable=False` hard signal provides a clean, bookable/unbookable
  distinction without overloading the `other` label.
- Critic LLM cost is incurred only on soft-only trips, not on the happy path.

## Alternatives considered

- **Keep label-based escalation** — simple, but causes the observed failure:
  legitimate documents dead-end on a label. Rejected.
- **Confidence-score-only gate** — supported by Document AI at the field level,
  but raw self-reported LLM confidence is nondeterministic and uncalibrated (Google
  guidance). Used as a *soft* input only, not as the sole gate. Rejected as sole
  mechanism.
- **Always-escalate on `other`** — the current behaviour. Rejected as too rigid:
  it conflates "I don't recognise the label" with "I cannot post this." These are
  different facts.
- **Single combined gate** — consolidating `detect_struggle` and `approval_gate`
  into one pass was explored and rejected; they guard different axes (extraction
  fidelity vs posting approval) and run at different points in the graph.
