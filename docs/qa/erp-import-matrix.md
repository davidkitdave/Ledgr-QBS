# ERP import matrix (manual)

Tick when a human has opened the Ledgr `.xlsx` in the real accounting software and
confirmed the import succeeds. Cannot be fully automated without ERP licenses.

Use with [light-path-live-smoke.md](light-path-live-smoke.md) section 8.

**Tester:** _______________ **Date:** _______________ **Environment:** dev / prod

---

## QBS Ledger

| Check | Pass |
|-------|------|
| Purchase invoice imports without column errors | [ ] |
| Sales invoice imports to sales module | [ ] |
| Tax amounts match source PDF (±0.01) | [ ] |
| Credit note amounts are negative | [ ] |
| FY workbook name `{Client} - Ledger_FY{fy}.xlsx` is correct | [ ] |

**Notes:**

---

## Xero

| Check | Pass |
|-------|------|
| `*ContactName`, `*InvoiceNumber`, `*InvoiceDate` populated | [ ] |
| `*TaxType` matches printed treatment | [ ] |
| Line amounts reconcile to invoice total | [ ] |
| Sales vs purchase routed correctly | [ ] |

**Notes:**

---

## AutoCount

| Check | Pass |
|-------|------|
| DocNo, TaxCode, TaxableAmt, Tax columns accepted | [ ] |
| Purchase vs sales document type correct | [ ] |
| Credit note sign correct | [ ] |

**Notes:**

---

## SQL Account

| Check | Pass |
|-------|------|
| `_DOCNO`, `_DATE`, `_TAXCODE`, `_TAXAMT` accepted | [ ] |
| Purchase vs sales routing correct | [ ] |

**Notes:**

---

## Known product gaps (light path)

- Account codes are **blank** on export until agent COA wiring lands (ADR-0036).
- Creditor/debtor codes may be empty for AutoCount/SQL Account.

Record any import blockers here for follow-up issues.
