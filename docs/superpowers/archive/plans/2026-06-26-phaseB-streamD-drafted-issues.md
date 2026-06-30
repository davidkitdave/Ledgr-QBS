# Drafted issues — Phase B (CashBook PV/OR) + Stream D (cutover)

Held from the **2026-06-26 grill-with-docs** session. Phase A (issues **#26–#29**) and
**B0 (#30)** were published; the slices below are **drafted but NOT yet published**, to avoid a
stale backlog. Publish them (in dependency order) once Phase A lands. Governed by
**ADR-0027 / ADR-0028 / ADR-0029**.

These are vertical tracer-bullet slices — each a thin end-to-end path, demoable on its own.

---

## Phase B — counterparty-code routing + CashBook PV/OR (per-target; ADR-0028, amended 2026-06-26)

**Re-scope (2026-06-26):** the Accounting Module driver is the **counterparty's Creditor/Debtor
code**, not `payment_status` (withdrawn — see amended ADR-0028). A bill that resolves to its code →
**AP/AR Invoice**; a one-off cash expense with **no** code → **CashBook PV/OR**. Payment itself is
bank-lane settlement, out of scope for the invoice path. These slices reflect that frame.

### B1 — Resolve counterparty → Creditor/Debtor code; seed from the client's balance report *(AFK)*
**Blocked by:** #26 (Phase A engine intelligence on the shared engine)

**What to build:** The universal core every AP/AR Invoice needs. Resolve a document's counterparty
to the client's own **Creditor/Debtor code** (Entity_Memory / Correction first), and let a client
**ingest their own Creditor Balance / Debtor Balance report** to bulk-seed those codes (same
soft-gate path as the COA, ADR-0006) — Ledgr never invents a code. A counterparty with no
resolvable code on a **credit** document takes **one** Review (HITL) pause to capture it, then is
remembered. No example client's codes are baked in.

**Acceptance criteria:**
- [ ] A document from a known counterparty resolves to the client's own code (no pause).
- [ ] Ingesting a Creditor/Debtor balance report bulk-seeds codes into Entity_Memory.
- [ ] A brand-new counterparty on a credit bill pauses once; the captured code is remembered.
- [ ] No generic/default or example-client codes anywhere in code or rule-data.

### B2 — No-counterparty paid expense → CashBook PV (AutoCount), end-to-end *(AFK)*
**Blocked by:** B1, #30 (confirmed PV/OR template columns), #28 (line-level eval)

**What to build:** The first CashBook vertical. A one-off **paid** purchase with **no resolvable
creditor code** (petty cash, a directly-paid utility) books to the **CashBook Payment Voucher
(PV)** module — straight to an expense GL account with a free-text payee — instead of an AP Invoice
that would leave a phantom creditor. Driven by the profile's **CashBook block**, not an ERP name:
routing = "counterparty resolves to a code → AP Invoice; else (paid, no code) → PV". The PV
exporter writes the correct sheet; Excel workbook, Slack preview and Job summary follow the
profile; one golden test locks the path.

**Acceptance criteria:**
- [ ] A paid AutoCount purchase with no resolvable creditor code books to CashBook PV (not AP Invoice).
- [ ] A bill that resolves to a creditor code still books to AP Invoice (unchanged).
- [ ] Excel sheet, Slack preview and Job summary reflect the PV module via the profile (no hardcoding).
- [ ] QBS/Xero clients unaffected (no CashBook block → everything posts to the direction sheet).
- [ ] Golden test covers the PV path; line-level eval green.

### B3 — No-counterparty paid receipt → CashBook OR (AutoCount) *(AFK)*
**Blocked by:** B2

**What to build:** The sales mirror — a one-off **paid** receipt with **no resolvable debtor code**
books to the **CashBook Official Receipt (OR)** module (income GL + free-text payer); a receipt
that resolves to a debtor code still books to AR Invoice.

**Acceptance criteria:**
- [ ] Paid AutoCount receipt with no debtor code → CashBook OR; resolved debtor → AR Invoice.
- [ ] Presentation surfaces follow the profile; golden test covers OR.

### B4 — SQL Account CashBook PV + OR *(AFK)*
**Blocked by:** B2

**What to build:** Declare the CashBook block in the SQL Account profile and confirm the PV/OR
routing works for the second ERP, reusing the generic routing from B2/B3.

**Acceptance criteria:**
- [ ] SQL Account: paid no-code purchase → PV; paid no-code receipt → OR; resolved counterparty → AP/AR.
- [ ] Golden tests cover SQL Account PV/OR; no ERP-name branching in code.

---

## Stream D — cutover (after Phase A engine work + line-eval green)

### D1 — Wire + verify credits / Slack delivery / HITL on the clean path *(AFK)*
**Blocked by:** #26 (Phase A engine intelligence lands on the shared engine)

**What to build:** Ensure the clean `ledgr_agent` document path (behind `LEDGR_USE_CLEAN_AGENT`)
charges credits, delivers to Slack, and pauses/resumes HITL via the existing Firestore bridge —
verified end-to-end with the flag on in a test, not in prod. HITL stays on the `hitl.py` bridge
(ADR-0026: **not** ADK Tool Confirmation, **not** a RequestInput node).

**Acceptance criteria:**
- [ ] A document processed on the clean path charges the correct credits (durable, idempotent).
- [ ] Delivery (Block Kit table + workbook) matches the legacy path output.
- [ ] A HITL pause on the clean path resumes correctly on Slack approve/edit.

### D2 — Flip `LEDGR_USE_CLEAN_AGENT` + soak + live QA *(HITL)*
**Blocked by:** D1, #28 (line-level eval green), #29 (SOA cover-skip)

**What to build:** The go-live decision. Flip the flag ON in prod once line-eval is green and the
clean path is verified; soak with both paths coexisting (**flag = instant rollback**); run live QA
on real documents. Human-gated.

**Acceptance criteria:**
- [ ] Line-level eval green; clean-path live QA on real SG/MY documents passes.
- [ ] Flag flipped ON; soak period defined; rollback (flip OFF) verified to work.

### D3 — Delete legacy `accounting_agents` graph after soak *(AFK)*
**Blocked by:** D2

**What to build:** After the soak signs off, delete the retired graph (`agent.py` graph,
`nodes.py` graph nodes) and retire the `LEDGR_USE_CLEAN_AGENT` flag. Keep `slack_runner` Slack
infra and the clean-agent branch.

**Acceptance criteria:**
- [ ] Legacy graph + flag removed; suite + line-eval still green.
- [ ] No references to the deleted graph remain; clean agent is the sole path.
