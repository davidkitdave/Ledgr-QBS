# Extraction Tournament Round 1

Client: Company-A
Hermetic: False
**Winner: V3**

## Ranking

| Variant | Avg score | Completeness | Reconcile | False splits |
|---------|-----------|--------------|-----------|--------------|
| V3 | 1.0 | 1.0 | 1.0 | 0 |

## Per fixture × variant

- **vendor_invoice_sample** / V3: score=1.0 completeness=1.0 docs=1 inv#=True total=True
- **vendor_invoice_d12** / V3: score=1.0 completeness=1.0 docs=1 inv#=True total=True
- **management_fees** / V3: score=1.0 completeness=1.0 docs=1 inv#=True total=True
- **expense_claim** / V3: score=1.0 completeness=1.0 docs=1 inv#=True total=True

## Proposed rubric (post-tournament)

- 50% header completeness (invoice #, date, lines, doc_total)
- 30% reconcile rate
- 20% line presence
- Penalty: −0.15 per false document split
- Penalty: −0.05 per needs_fx_review flag

