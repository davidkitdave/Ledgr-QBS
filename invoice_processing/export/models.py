"""Normalized invoice model shared by the tax classifier and exporters.

This is the single intermediate representation between document extraction and the
accounting-import exporters. Each `InvoiceLine` is one (description, tax-treatment)
pair — a telco bill's SR and ZR portions are two separate lines, mirroring the
one-row-per-treatment behaviour of the target import files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class PartyInfo:
    """A supplier (for purchases) or customer (for sales)."""

    name: Optional[str] = None
    country: Optional[str] = None          # "SG" or an overseas country/None if unknown
    gst_regno: Optional[str] = None        # GST registration number / UEN if shown
    email: Optional[str] = None

    @property
    def gst_registered(self) -> bool:
        """Whether a GST registration number is shown on the document."""
        return bool(self.gst_regno and str(self.gst_regno).strip())

    @property
    def is_overseas(self) -> Optional[bool]:
        if not self.country:
            return None
        return self.country.strip().upper() not in ("SG", "SINGAPORE")


@dataclass
class InvoiceLine:
    """One line item / charge. After classification, `tax_treatment` is set."""

    description: str
    quantity: Optional[float] = None
    unit_amount: Optional[float] = None    # unit price, ex-tax
    net_amount: Optional[float] = None     # line amount, ex-tax
    gst_amount: Optional[float] = None     # GST on this line (0 for ZR/ES/OS)
    account_code: Optional[str] = None     # COA code from categorization (later phase)
    item_code: Optional[str] = None
    tax_keyword: Optional[str] = None      # explicit per-line tax wording from extraction (e.g. "SR","ZR","9%","0%","exempt") — a hint for the tax classifier

    # Set by the tax classifier:
    tax_treatment: Optional[str] = None    # canonical code: SR/ZR/ES/OS/IM/NT
    tax_confidence: Optional[float] = None
    tax_flagged: bool = False
    tax_reason: Optional[str] = None       # which rule/signal decided it


@dataclass
class NormalizedInvoice:
    """Direction-aware invoice. `doc_type` is 'purchase' or 'sales'."""

    doc_type: str = "purchase"             # 'purchase' (supplier inv/receipt) | 'sales'
    invoice_number: Optional[str] = None
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    currency: str = "SGD"
    po_number: Optional[str] = None

    supplier: PartyInfo = field(default_factory=PartyInfo)   # used when doc_type=purchase
    customer: PartyInfo = field(default_factory=PartyInfo)   # used when doc_type=sales

    lines: list[InvoiceLine] = field(default_factory=list)

    # Invoice-level (document) grand totals, carried from ExtractedInvoice in to_normalized.
    # Authoritative source for the exporter's Total column; None falls back to Σ lines.
    doc_subtotal: Optional[float] = None    # ex-GST grand total from the bill
    doc_gst_total: Optional[float] = None   # GST grand total from the bill
    doc_total: Optional[float] = None       # grand total (subtotal + gst) from the bill

    # The CLIENT's own GST registration status (from Client Setup TAX_REGISTERED).
    our_gst_registered: bool = True

    # FX / multi-currency fields (set by to_normalized when currency != base_currency).
    # fx_rate: exchange rate applied to convert doc amounts to base currency.
    #   - 1.0 when doc currency == base currency (no conversion needed).
    #   - None when currency != base currency and no rate was derivable — see needs_fx_review.
    # original_currency / original_total: the raw doc currency + total BEFORE conversion.
    # needs_fx_review: True when doc is non-base-currency and no fx_rate could be derived;
    #   the doc must NOT be silently booked at rate=1 — it is flagged for human review.
    fx_rate: Optional[float] = 1.0
    original_total: Optional[float] = None
    original_currency: Optional[str] = None
    needs_fx_review: bool = False

    # Reconciliation of the ledger lines against the document totals (set in to_normalized).
    reconciled: bool = True
    reconcile_note: Optional[str] = None

    @property
    def counterparty(self) -> PartyInfo:
        """The other party: supplier for purchases, customer for sales."""
        return self.supplier if self.doc_type == "purchase" else self.customer


@dataclass
class BankTransaction:
    """One bank-statement transaction row — kept distinct (never collapsed)."""

    date: Optional[date] = None
    description: str = ""
    bank_ref: Optional[str] = None
    withdrawal: Optional[float] = None   # debit / money out (positive)
    deposit: Optional[float] = None      # credit / money in (positive)
    balance: Optional[float] = None      # running balance after this txn
    math_ok: Optional[bool] = None       # set by reconcile_running_balance
    note: Optional[str] = None


@dataclass
class BankStatement:
    """A bank/account statement — one per Excel sheet."""

    bank_name: str = ""                  # -> Excel sheet title (e.g. "OCBC - 5001")
    account_number: Optional[str] = None
    currency: str = "SGD"
    statement_period: Optional[str] = None
    opening_balance: Optional[float] = None
    closing_balance: Optional[float] = None
    transactions: list[BankTransaction] = field(default_factory=list)
    source_file_id: Optional[str] = None
    extract_mode: Optional[str] = None   # "digital" (pdfplumber) or "vision"
    reconciled: bool = True
    reconcile_note: Optional[str] = None
