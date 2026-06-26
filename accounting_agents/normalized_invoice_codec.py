"""Robust serialization and deserialization for `NormalizedInvoice` and `BankStatement`.

This module is the single source of truth for converting invoice and bank-statement
domain objects to and from the plain dictionaries that ADK persists in session state
(and Firestore). The previous inline codec in `accounting_agents/nodes.py` silently
dropped `tax_visible_on_document` and `direction_reason` — fields that ADR-0014/0015
made mandatory for downstream categorization, tax classification, and HITL review.

Properties guaranteed by this codec:

* Round-trip fidelity: every field on `NormalizedInvoice`, `InvoiceLine`,
  `BankStatement`, and `BankTransaction` survives a `to_dict` -> `from_dict`
  round trip (asserted by `tests/test_nodes.py::test_normalized_invoice_codec_*`).
* Backward compatibility: the on-the-wire dict shape matches the prior
  `_inv_to_dict` / `_dict_to_inv` / `_bank_to_dict` / `_dict_to_bank` exactly
  (including nested `supplier` / `customer` / `lines` / `transactions` keys),
  so persisted Firestore documents remain readable after the upgrade.
* Defensive defaults: missing optional fields fall back to the dataclass
  defaults, never raise. This keeps the codec safe against older persisted
  sessions that pre-date a field's introduction.
"""

from __future__ import annotations

from dataclasses import asdict, fields as dc_fields
from datetime import date, datetime
from typing import Any, Optional

from invoice_processing.export.models import (
    BankStatement,
    BankTransaction,
    InvoiceLine,
    NormalizedInvoice,
    PartyInfo,
)

_ISO_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%Y/%m/%d",
    "%m/%d/%Y",
)


def _parse_iso(value: Any) -> Optional[date]:
    """Parse an ISO-or-close date string into a `date`, or `None` if unparseable."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in _ISO_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _party_from_dict(d: Optional[dict[str, Any]]) -> PartyInfo:
    """Build a `PartyInfo` from a dict, tolerating missing or extra keys."""
    if not d:
        return PartyInfo()
    return PartyInfo(
        name=d.get("name"),
        country=d.get("country"),
        gst_regno=d.get("gst_regno"),
        email=d.get("email"),
        vendor_code=d.get("vendor_code"),
    )


def _line_from_dict(d: dict[str, Any]) -> InvoiceLine:
    """Build an `InvoiceLine` from a dict, ignoring unknown keys defensively."""
    known = {f.name for f in dc_fields(InvoiceLine)}
    return InvoiceLine(**{k: v for k, v in d.items() if k in known})


# --------------------------------------------------------------------------- #
# NormalizedInvoice <-> dict
# --------------------------------------------------------------------------- #


def invoice_to_dict(inv: NormalizedInvoice) -> dict[str, Any]:
    """Serialize a `NormalizedInvoice` to a plain dict (Firestore-safe)."""
    d = asdict(inv)
    # `date` instances are not JSON-serialisable; convert to ISO strings.
    d["invoice_date"] = inv.invoice_date.isoformat() if inv.invoice_date else None
    d["due_date"] = inv.due_date.isoformat() if inv.due_date else None
    if inv.page_range is not None:
        d["page_range"] = [inv.page_range[0], inv.page_range[1]]
    else:
        d["page_range"] = None
    return d


def _page_range_from_dict(value: Any) -> Optional[tuple[int, int]]:
    if not value or not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    return int(value[0]), int(value[1])


def dict_to_invoice(d: dict[str, Any]) -> NormalizedInvoice:
    """Deserialize a dict (from session state / Firestore) to a `NormalizedInvoice`.

    Preserves every field on the dataclass, including the previously-dropped
    `tax_visible_on_document` and `direction_reason` (see ADR-0014 / ADR-0015).
    """
    return NormalizedInvoice(
        doc_type=d.get("doc_type", "purchase"),
        invoice_number=d.get("invoice_number"),
        invoice_date=_parse_iso(d.get("invoice_date")),
        due_date=_parse_iso(d.get("due_date")),
        currency=d.get("currency") or "",
        po_number=d.get("po_number"),
        supplier=_party_from_dict(d.get("supplier")),
        customer=_party_from_dict(d.get("customer")),
        lines=[_line_from_dict(ld) for ld in (d.get("lines") or [])],
        doc_subtotal=d.get("doc_subtotal"),
        doc_gst_total=d.get("doc_gst_total"),
        doc_total=d.get("doc_total"),
        our_gst_registered=bool(d.get("our_gst_registered", True)),
        fx_rate=d.get("fx_rate"),
        original_total=d.get("original_total"),
        original_currency=d.get("original_currency"),
        needs_fx_review=bool(d.get("needs_fx_review", False)),
        reconciled=bool(d.get("reconciled", True)),
        reconcile_note=d.get("reconcile_note"),
        tax_visible_on_document=d.get("tax_visible_on_document"),
        direction_reason=d.get("direction_reason"),
        document_kind=d.get("document_kind"),
        page_range=_page_range_from_dict(d.get("page_range")),
        source_file_id=d.get("source_file_id"),
    )


# --------------------------------------------------------------------------- #
# BankStatement <-> dict
# --------------------------------------------------------------------------- #


def bank_to_dict(stmt: BankStatement) -> dict[str, Any]:
    """Serialize a `BankStatement` to a plain dict (Firestore-safe)."""
    d = asdict(stmt)
    d["transactions"] = [
        {**asdict(t), "date": t.date.isoformat() if t.date else None}
        for t in stmt.transactions
    ]
    return d


def _txn_from_dict(d: dict[str, Any]) -> BankTransaction:
    """Build a `BankTransaction` from a dict, ignoring unknown keys defensively."""
    known = {f.name for f in dc_fields(BankTransaction)}
    td = {k: v for k, v in d.items() if k in known}
    td["date"] = _parse_iso(td.get("date"))
    return BankTransaction(**td)


def dict_to_bank(d: dict[str, Any]) -> BankStatement:
    """Deserialize a dict (from session state / Firestore) to a `BankStatement`."""
    known = {f.name for f in dc_fields(BankStatement)} - {"transactions"}
    fields = {k: v for k, v in d.items() if k in known}
    txns = [_txn_from_dict(t) for t in (d.get("transactions") or [])]
    return BankStatement(transactions=txns, **fields)


# --------------------------------------------------------------------------- #
# Public helpers exposed to nodes.py (back-compat shims preserve call sites)
# --------------------------------------------------------------------------- #


__all__ = [
    "invoice_to_dict",
    "dict_to_invoice",
    "bank_to_dict",
    "dict_to_bank",
    "_parse_iso",
]