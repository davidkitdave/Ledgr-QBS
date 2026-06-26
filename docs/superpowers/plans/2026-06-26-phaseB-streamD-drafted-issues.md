# Drafted issues — Phase B (CashBook PV/OR) + Stream D (cutover)

Held from the **2026-06-26 grill-with-docs** session. Phase A (issues **#26–#29**) and
**B0 (#30)** were published; the slices below are **drafted but NOT yet published**, to avoid a
stale backlog. Publish them (in dependency order) once Phase A lands. Governed by
**ADR-0027 / ADR-0028 / ADR-0029**.

These are vertical tracer-bullet slices — each a thin end-to-end path, demoable on its own.

---

## Phase B — CashBook PV/OR (per-ERP; ADR-0028)

### B1 — Paid AutoCount purchase → CashBook PV, end-to-end *(AFK)*
**Blocked by:** #30 (confirmed PV/OR template columns), #28 (line-level eval)

**What to build:** The first full vertical path for the Accounting Module fork. A paid purchase
for an AutoCount client books to the **CashBook Payment Voucher (PV)** module instead of AP
Invoice, cutting through every layer: read `payment_status` (paid|credit|unknown) on the
document; AutoCount profile declares a **CashBook block**; deterministic profile-driven routing
sends `paid → PV`, `credit → AP`; the PV exporter writes the correct sheet; the Excel workbook,
Slack preview and Job summary follow the profile; one golden test locks the path. Generic routing
— **no ERP names in code**.

**Acceptance criteria:**
- [ ] A paid AutoCount purchase document books to CashBook PV (not AP Invoice), end-to-end.
- [ ] A credit AutoCount purchase still books to AP Invoice (unchanged).
- [ ] Excel sheet, Slack preview columns, and Job summary all reflect the PV module via the profile (no hardcoding).
- [ ] QBS/Xero clients are unaffected (regression check — they have no CashBook block).
- [ ] Golden test covers the PV path; line-level eval green.

### B2 — Paid AutoCount sale → Official Receipt (OR) *(AFK)*
**Blocked by:** B1

**What to build:** Extend the same machinery to the sales side — a paid sale books to the
**CashBook Official Receipt (OR)** module; a credit sale still books to AR Invoice.

**Acceptance criteria:**
- [ ] Paid AutoCount sale → CashBook OR; credit sale → AR Invoice.
- [ ] Presentation surfaces follow the profile; golden test covers OR.

### B3 — SQL Account PV + OR *(AFK)*
**Blocked by:** B1

**What to build:** Declare the CashBook block in the SQL Account profile and confirm the PV/OR
routing works for the second ERP, reusing the generic routing built in B1/B2.

**Acceptance criteria:**
- [ ] Paid SQL Account purchase → PV; paid sale → OR; credit → AP/AR.
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
