# 0031 — Light path: one LLM read + minimum deterministic policy ladder

- **Status:** Accepted (direction); ladder rounds empirically gated
- **Date:** 2026-06-27
- **Deciders:** Ledgr team
- **Relates to:** ADR-0030 (direct-call read beats chunked factory), ADR-0026
  (AI reads, rules apply), ADR-0006 (per-client COA), ADR-0011 (Understand layer),
  ADR-0025 (COA confidence). **Does not replace** ADR-0030 — it defines what
  happens *after* the read.
- **Evidence:** `ledgr_agent/tools/light_ledger.py`,
  `scripts/spike_light_vs_factory.py`,
  `~/Desktop/localtest/BV-0002830_Starhub_*_custom_light_r{2,3}.json`

## Context

ADR-0030 proved the **read** shape: one direct `generate_content` call on the
whole PDF returns Drive-quality summaries (1 doc, 3 telco lines, clean
`tax_lines[]`) in ~8 s. The old chunked factory on the same Starhub bill
returned 12 fake docs / 216 noisy lines in ~210 s.

The open question is not "can the LLM read?" but **"what is the smallest
policy chain that still produces bookable rows?"** The heavy
`process_document_batch` path runs classify → categorize → route → export inside
a large extraction factory (chunking, faithful-capture prompts, multi-pass
merges). Much of that complexity may be unnecessary once the read is clean.

The **light path** is a deliberate experiment: prove the minimum rule ladder
on 4–5 real fixtures before migrating production.

## Decision

**1. Light path shape — thin spine, not a second factory**

| Round | What runs | Who decides |
|---|---|---|
| **R1 — Read** | One Gemini call → generic `LedgerRowBundle` (vendor, refs, `tax_lines[]`, summary `rows[]`) | LLM reads the document |
| **R2 — Tax** | Bridge doc `tax_lines[]` + vendor UEN onto lines; then `classify_invoice` | **Deterministic** tax rules (YAML-backed); LLM does **not** pick tax codes |
| **R3 — COA** | `categorize_invoice` against the **client's own COA** | **Entity memory / corrections first** (deterministic); then **one batched LLM call** picks `account_code` from the COA list for unresolved lines |
| **R4 — Deliver** | `route_document` + `QbsLedgerExporter` / `tagged_export_rows` | Deterministic FY routing + per-target projection |

Rounds are added **one at a time** only when the A/B spike (`spike_light_vs_factory.py`)
shows a field mismatch that the previous round cannot fix. Stop at the first round
that matches or beats the factory on eval fixtures.

**2. COA — LLM thinks from the client's list; no keyword hacks in the light path**

Round 1 **must not** guess account codes or category names ("Internet Services",
"Mobile Services"). The read schema leaves `account_code` null.

Round 3 uses the **existing** `categorize_invoice` contract (ADR-0006,
CONTEXT [[Categorisation]]):

- Remembered vendor / entity memory → deterministic mapping when present.
- Whatever remains → **one structured LLM call** with the client's COA JSON
  and line descriptions; the model must return a key from that list (or
  `UNMAPPED`).
- Low-confidence lines → flag → [[Review (HITL)]] → [[Correction]].

**Explicitly rejected for the light spike:** rewriting COA keywords, comma-splitting
space-separated keyword fields, or other normalisation tricks to force deterministic
keyword hits. If keyword matching in the shared categorizer is weak, that is a
**separate** engine fix — not a workaround in `light_ledger.py`.

**3. Tax — rules apply after the read, not during**

The read captures printed `tax_lines[]` and line nets. A small deterministic
bridge (`_enrich_lines_for_tax`) spreads doc-level GST buckets onto lines so
`classify_invoice` can run the existing purchase decision table (supplier UEN,
per-line `gst_amount`, `tax_keyword`). The LLM does **not** assign final
`tax_treatment` — that stays ADR-0026 deterministic.

**4. Production cutover stays eval-gated**

`light_process` and `spike_light_vs_factory.py` are **additive** until the
ladder wins on the golden fixture set. `process_document_batch` remains the
production path. `extract_one_bill_minimal` (ADR-0030) is the read-only fast
path; light R1–R4 is the **booked-rows** experiment.

## Verified so far (Starhub bill, 2026-06-27)

| Round | Result | vs factory |
|---|---|---|
| R1 | 1 doc, 3 summary lines, clean `tax_lines[]`, ~8 s, 1 Gemini call | Factory: 12 docs, 216 lines, ~210 s |
| R2 | `tax_treatment`: SR / SR / ZR; GST 15.25 / 89.55 / 0 | Factory: empty tax on all lines |
| R3 | COA via `categorize_invoice` LLM from `playground_profile.json` COA | Factory: one line `7000` on a summary row |

Light is **faster** and **better on tax** for this fixture. COA quality depends on
the client COA seeded in profile (playground motor-workshop chart ≠ telco — both
paths struggle; the ladder step itself works).

## Next build plan (ordered)

1. **Wire light R1–R4 behind a feature flag** on `process_document_batch`
   (or a sibling `process_document_light` tool) — same outer batch shape, swap
   only the read + policy spine.
2. **Golden eval gate** — extend `tests/eval` / `spike_light_vs_factory.py`
   fixtures: bill (SR/ZR telco), receipt, credit note, expense claim, bank
   statement; record winning round per doc type in `light_vs_factory_minimum.md`.
3. **COA quality** — ensure real client profiles (not demo motor COA) in eval;
   optional: pass COA list into R1 prompt as *read-only context* for line
   descriptions only — **still assign codes in R3**, not in R1.
4. **R4 export parity** — QBS column fill (`Invoice Date`, `Entity Tax ID`,
   currency `SGD`, `Tax Amount` from classifier); compare to factory export rows.
5. **`document_truth` QA hook** — use `ledgr_agent/tools/document_truth.py` in
   the spike to flag missing invoices / amount coverage independent of LLM
   (multi-invoice SOA packages).
6. **Retire chunk-first extraction** for single bills once eval passes (ADR-0030
   amendment already narrows chunking); keep chunked path only for genuine
   multi-doc fan-out (ADR-0029).

## Consequences

- The migration target is **not** "rebuild the Workflow graph" — it is
  **one read call + the smallest proven policy chain**.
- COA intelligence lives in the **existing categorizer LLM** (client list in
  prompt), not in extraction guesses or keyword patches.
- Tax intelligence lives in **deterministic rules + printed tax breakdown**, not
  in the read model's `tax_treatment` field (R1 may leave it null; R2 sets it).
- Factory complexity (chunking, faithful-capture, merge) is **suspect** until
  the ladder proves it necessary per doc type.

## Alternatives considered

- **LLM picks tax and COA in the read call** — rejected (ADR-0026; not
  auditable; eval showed read already captures `tax_lines[]` well).
- **Keyword-normalise COA for deterministic match in the spike** — rejected;
  masks categorizer gaps and bypasses the intended "LLM from client list" path.
- **Skip ladder, migrate whole factory at once** — rejected; no evidence for
  which policy steps are actually needed per doc type.
