# 0027 — Direction is read by the LLM, with the Client's vendor role as a deterministic floor

- **Status:** Accepted
- **Date:** 2026-06-26
- **Deciders:** Ledgr team
- **Relates to:** ADR-0026 (AI reads, rules apply), ADR-0004 (learning via structured
  corrections), ADR-0017 (HITL signal taxonomy), ADR-0024 (auto-book the routine case,
  escalate only genuine ambiguity)

## Context

"Direction" — whether a document is a **purchase** or a **sale** in the Client's books —
was historically resolved two ways: the legacy Slack graph used the extraction LLM's
`direction_for_client`, while the clean agent used a `difflib` name matcher that silently
defaulted unknowns to "purchase" (a latent mis-book). A.4 (2026-06-25) unified both on the
LLM path: the LLM reads direction, and `unknown` / `self_referential` now flag HITL instead
of a silent purchase.

That fixed the silent mis-book but left a live annoyance: on a third-party retail receipt the
Client's name is not printed, so the LLM correctly abstains (`unknown`) → a Review pause —
*every time*, even for a vendor the Client has bought from many times. The question: remove
the *unnecessary* pauses without reintroducing a brittle rule or letting the LLM silently
guess.

A blunt "receipt ⇒ purchase" rule was rejected up front: a Client that issues its own receipts
(cash sales) would have those mis-booked as purchases. Direction is fundamentally about **who
issued the document**, which is a *reading* decision — not an apply-layer policy.

## Decision

**Direction is a reading decision owned by the LLM, with the Client's own recorded vendor
*role* as a deterministic floor that fills gaps and flags conflicts — never a silent
override.**

1. The Understand layer reads `direction_for_client` (`purchase | sales | self_referential |
   unknown`).
2. For a vendor present in the Client's `entity_memory` with a **role** (Creditor = buy-from
   ⇒ purchase; Debtor = sell-to ⇒ sales), that role is a deterministic prior:
   - LLM `unknown` + role present ⇒ take the role's direction (**no pause**).
   - LLM agrees with the role ⇒ proceed.
   - LLM **confidently disagrees** with the role ⇒ **Review (HITL)** (a genuine conflict —
     role-flip, credit note, or stale record); the human's fix updates the record.
3. A **brand-new vendor with no recorded role** takes **one** Review pause; the answer is
   remembered (role recorded) and the floor handles that vendor thereafter.
4. The floor **rides on buy/sell role only** — it never uses an ERP's account codes — so it is
   identical for every accounting target (Xero/QBS and AutoCount/SQL alike). When a Client has
   no roles recorded, the floor has no data and degrades to the pure LLM read.
5. We do **not** enrich the extraction prompt for the new-vendor case yet (a prompt change
   triggers the eval gate). We measure how often first-time receipts actually pause and add
   generic prompt reasoning only if warranted ("measure before adding logic").

This is the same "apply what's learned, surface real conflicts" shape already used for COA
[[Categorisation]] (ADR-0004), extended to Direction. It is **Branch-C compliant**: the floor
is a *data-lookup over the Client's own authoritative records*, not semantic keyword guessing.

## Consequences

- Repeat pauses on taught vendors disappear; direction is reproducible for known vendors
  (auditor-friendly, run-to-run stable).
- A wrong/stale record or a role-flip surfaces as a Review, not a silent mis-book.
- New vendors cost exactly **one** teaching pause, consistent with the Familiarity/Correction
  learning system.
- No brittle keyword rule is introduced.

## Alternatives considered

- **"receipt ⇒ purchase" policy rule** — mis-books a Client's own cash-sale receipts; rejected
  on correctness.
- **Floor always overrides the LLM** — silently mis-books role-flips / credit notes; rejected.
- **Pure LLM, no floor** — repeat pauses on taught vendors + non-reproducible direction across
  runs; rejected.
- **Prompt-enrich now for new vendors** — deferred (eval-gate cost) pending measurement.

## Sources

- ADK / Gemini — `LlmAgent` reads via structured output; deterministic logic for predictable
  outcomes (see ADR-0026 sources). Verified via the ADK-docs and Google developer-knowledge
  MCPs (2026-06-26): deterministic Python for reproducible routing, the LLM for reading.
