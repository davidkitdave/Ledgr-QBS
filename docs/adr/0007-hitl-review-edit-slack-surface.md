# 0007 — HITL review & edit in Slack: one Job summary, threaded review cards, modal edits

> **ℹ️ Amended by [ADR-0026](0026-ai-reads-rules-apply-on-a-lean-llmagent.md) (2026-06-24).**
> This Slack review/edit surface is **retained unchanged**. Only the upstream *pause* changes:
> reviews are now triggered by the batch tool's `pending_reviews` (resumed via the `hitl.py`
> Firestore bridge), not by an ADK `RequestInput` graph node (graph retired). Edits still
> become Corrections (ADR-0004).

- **Status:** Accepted (surface retained; pause primitive amended by ADR-0026)
- **Date:** 2026-06-14
- **Deciders:** Ledgr team; grounded in Slack Block Kit docs + ADK HITL docs (via the adk-docs / google-dev-knowledge MCPs).

## Context

ADR-0003 chose ADK `RequestInput` as the pause/resume HITL primitive and described
the net-new work as "wiring the Bolt action handler and the review card." Live
testing of a 10-document drop exposed three gaps in that card/handler layer — the
*primitive* is sound; its Slack presentation is not:

1. **Channel spam.** Each document runs in its own session (a deliberate concurrency
   fix) and posts its own status + own review card. Ten documents → a wall of
   near-identical "Processed" / "Needs your review" messages.
2. **Cards don't identify their document.** `approval_card_blocks` (`app/blocks.py`)
   renders only a generic header + reason bullets. The only label on a reason is the
   *extracted* identity ("I-Receipt #1"), never the uploaded filename — so a human
   cannot tell which file a card refers to.
3. **"Edit" cannot edit.** The `edit` Bolt action (`accounting_agents/slack_runner.py`)
   resumes with `edits=None` and posts "Approved with edits" while changing nothing.
   Slack also forbids the obvious inline fix: `input` blocks are permitted **only in
   modals and the App Home tab**, never in channel messages. And no node reads
   `ApproveDecision.edits` even when it is supplied.

We researched Slack's interactive surfaces and ADK's edit-carrying resume to decide
the *presentation and edit-collection* layer that sits on top of ADR-0003.

## Decision

**One Job summary, threaded reviews.** A drop of N documents posts a single
live-updating **Job summary** message ([[Batch (Job)]]) — "N docs: X posted to
`<software>` FY`yyyy`, Y need review". Per-document review cards are posted as
**threaded replies** under that summary, each naming its **uploaded file**
(filename · vendor · amount · flag reason) with a permalink. This replaces
one-card-per-document. Per-document sessions and the serialized ledger write
(the concurrency fix) are unchanged — only the presentation aggregates.

**Edit = a Block Kit modal.** The `edit` action calls `views.open` synchronously on
click (the click mints a fresh `trigger_id`; the age of the paused approval is
irrelevant, so a card that sat for minutes still opens an editor). The modal is
pre-filled with the flagged line(s): account code as a `static_select` over the
client's COA, tax code as a select, amounts as `number_input` — one input group per
flagged line (≤100 blocks/view; page with `views.push` beyond that). On
`view_submission` the handler reads `view.state.values`, builds the `edits` dict,
and resumes with `ApproveDecision(decision="edit", edits=…)`.

**The engine applies edits; an edit becomes a Correction.** The post-gate path must
read `decision.edits` and apply it to the rows before consolidate/deliver (today it
does not). A field correction (vendor → account/tax code) is persisted as a
per-client [[Correction]] (ADR-0004) so the next document from that vendor is
categorised automatically.

**App Home queue deferred, not rejected.** A cross-client App Home review queue is
the right tool when a firm works many channels, but it is **purely additive** — the
same modal and the same resume path, just another launcher — so it is deferred until
multi-client scale earns it.

## Consequences

- The 10× channel spam collapses to one Job summary + a tidy review thread; the
  channel stays the system of record (ADR-0002).
- "Edit" becomes real structured correction, closing the loop ADR-0003 intended.
- Net-new work is the UI/handler (job summary, threaded cards, the edit modal +
  `view_submission` handler) plus a small engine step to apply `edits`. The
  `RequestInput` / `hitl.py` pause-resume machinery and the `ApproveDecision.edits`
  schema already exist.
- Modal editing only works while the bot answers the click within ~3s — open the
  modal synchronously (open a light "Loading…" view and `views.update` if datastore
  reads are slow).

## Alternatives considered

- **Inline editable fields in the channel message** — impossible: Slack `input`
  blocks aren't permitted in messages; rejected by the platform, not by preference.
- **App Home review queue now** — better at multi-client scale, but per-user/private
  and detached from the document's channel context; deferred as an additive future
  surface.
- **One card per document (status quo)** — the spam being fixed; rejected.
