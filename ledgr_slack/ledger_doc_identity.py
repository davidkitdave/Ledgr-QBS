"""Per-document ledger dedupe identity (WS-5.4 / M3).

The research spec names ``file_id + page_range + reference``, but the Slack
``file_id`` rotates on every re-upload.  Keys therefore use ``sheet`` +
``reference`` + ``page_range`` only so re-dropping the same PDF stays
idempotent (ADR-0010).  ``page_range`` disambiguates N documents from one
multi-invoice PDF when references would otherwise collide.

Row-signature fallback (issue #34)
----------------------------------
Some ERP import layouts carry **no readable invoice-identity column** for a
given direction — notably **AutoCount sales (AR Invoice)**, whose only doc
identifier (``DocNo``) is the constant ``<<New>>`` and which has no
``SupplierInvoiceNo``/invoice-number column at all.  For those sheets the
appended ``invoice_number`` cannot be recovered from the cleared Excel row, so
``remove_rows_for_month`` could not reconstruct the dedupe key and silently
skipped the ``seen_doc_keys`` purge — making a re-upload of the same invoice
look like a duplicate.

The fix gives those sheets a **row signature** built only from columns the
Excel already carries (``DocDate`` / debtor (or creditor) code / ``Amount``),
computed the SAME way at append time (from the exporter row) and at clear time
(from the Excel cells), via :func:`ledger_row_signature`.  Every other ERP
(QBS, Xero, SQL Account) and AutoCount **purchase** keeps its existing
``invoice_number`` / ``SupplierInvoiceNo`` identity — see
:func:`sheet_lacks_invoice_identity_column`.

Known limitation (accepted per issue #34): two genuinely distinct sales
invoices to the same debtor, on the same date, for the same amount collide.
No better identity exists in the AutoCount sales Excel; column changes are
deferred to issue #30.
"""

from __future__ import annotations

from typing import Any


def ledger_doc_identity(
    sheet: str,
    reference: str | None,
    page_range: tuple[int, int] | None = None,
    *,
    index: int = 0,
) -> str:
    """Return the Firestore ``seen_doc_keys`` entry for one ledger document."""
    ref = (reference or "").strip() or f"i{index}"
    if page_range is not None:
        start, end = page_range
        return f"{sheet}:{ref}:{start}-{end}"
    return f"{sheet}:{ref}"


def _normalize_amount(value: Any) -> str:
    """Canonical text for an amount so ``500.0`` (append, float) and ``500``
    (clear, int after openpyxl round-trips the cell) hash identically.

    Non-numeric / empty values fall back to a stripped string so the signature
    never raises; this keeps append and clear in lockstep even on odd cells.
    """
    if value is None:
        return ""
    try:
        return repr(float(value))
    except (TypeError, ValueError):
        return str(value).strip()


def ledger_row_signature(
    sheet: str,
    doc_date: Any,
    party_code: Any,
    amount: Any,
    *,
    index: int = 0,
) -> str:
    """Dedupe key for a sheet with no readable invoice-identity column (#34).

    Derived ONLY from values the Excel import row already carries so it is
    reproducible at clear time:

        ``{sheet}:sig:{DocDate}|{party_code}|{Amount}``

    ``party_code`` is the debtor (sales) or creditor (purchase) code.  Empty /
    ``None`` cells normalize to an empty token (openpyxl reads a blank cell back
    as ``None``, whereas the exporter emits ``""`` — both must collapse to the
    same token or append and clear diverge).  ``amount`` is normalized via
    :func:`_normalize_amount` for the same reason.

    This signature MUST be computed identically on both sides — that is the
    whole point of centralizing it here; see the module docstring.
    """
    date_part = "" if doc_date is None else str(doc_date).strip()
    code_part = "" if party_code is None else str(party_code).strip()
    amount_part = _normalize_amount(amount)
    sig = f"{sheet}:sig:{date_part}|{code_part}|{amount_part}"
    # Guard the degenerate all-empty row so distinct empty rows do not all
    # collapse onto one key (keeps parity with ledger_doc_identity's i{index}).
    if not (date_part or code_part or amount_part):
        return f"{sig}:i{index}"
    return sig


def sheet_lacks_invoice_identity_column(exporter: Any, doc_type: str) -> bool:
    """True when *exporter* has no readable invoice-identity column for *doc_type*.

    This is the single guard that scopes the row-signature fallback (#34): it is
    True only for AutoCount **sales** among the supported ERPs (QBS / Xero / SQL
    Account sales resolve ``invoice_number``; AutoCount purchase resolves
    ``SupplierInvoiceNo``).  Mirrors ``_invoice_identity_column``'s field order.
    """
    if not hasattr(exporter, "column_for_field"):
        return False
    for field in ("invoice_number", "supplier_invoice_no"):
        if exporter.column_for_field(field, doc_type):
            return False
    return True


def _doc_type_for_sheet(sheet: str) -> str:
    return "sales" if sheet == "Sales" else "purchase"


def ledger_doc_key_for_invoice(
    exporter: Any,
    sheet: str,
    inv: Any,
    index: int,
) -> str:
    """Append-side dedupe key for one invoice row (centralized for #34).

    Returns the row-signature fallback when *sheet* has no readable
    invoice-identity column (AutoCount sales), else the usual
    ``invoice_number`` + ``page_range`` identity.  The signature is read from
    the SAME exporter row that gets written to the Excel, so clear time can
    reconstruct it from those cells.
    """
    doc_type = _doc_type_for_sheet(sheet)
    if sheet_lacks_invoice_identity_column(exporter, doc_type):
        row = exporter.rows([inv], doc_type)[0]
        date_col = exporter.column_for_field("invoice_date", doc_type)
        code_field = "debtor_code" if doc_type == "sales" else "creditor_code"
        code_col = exporter.column_for_field(code_field, doc_type)
        amount_col = exporter.column_for_field("sub_total", doc_type)
        return ledger_row_signature(
            sheet,
            row.get(date_col) if date_col else None,
            row.get(code_col) if code_col else None,
            row.get(amount_col) if amount_col else None,
            index=index,
        )
    return ledger_doc_identity(
        sheet, inv.invoice_number, getattr(inv, "page_range", None), index=index
    )
