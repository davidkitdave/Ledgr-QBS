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

_HOME_COUNTRY_EQUIVALENTS: dict[str, set[str]] = {
    "SG": {"SG", "SGP", "SINGAPORE"},
    "MY": {"MY", "MYS", "MALAYSIA", "MSIA", "M'SIA"},
}


@dataclass
class PartyInfo:
    """A supplier (for purchases) or customer (for sales)."""

    name: Optional[str] = None
    country: Optional[str] = None          # "SG" or an overseas country/None if unknown
    gst_regno: Optional[str] = None        # GST registration number / UEN if shown
    email: Optional[str] = None
    vendor_code: Optional[str] = None      # creditor code for purchases (from entity memory)

    @property
    def gst_registered(self) -> bool:
        """Whether a GST registration number is shown on the document."""
        return bool(self.gst_regno and str(self.gst_regno).strip())

    @property
    def is_overseas(self) -> Optional[bool]:
        """Whether this party is outside Singapore (legacy default home country).

        .. deprecated::
            Prefer :meth:`is_overseas_for` with the client's home country so MY
            clients do not treat SG suppliers as domestic.

        WARNING: hard-coded to SG.  Callers that serve non-SG clients MUST use
        ``is_overseas_for(home_country)`` instead, or SG suppliers will be
        incorrectly treated as domestic for e.g. a Malaysian client.
        """
        return self.is_overseas_for("SG")

    def is_overseas_for(self, home_country: str) -> Optional[bool]:
        """True when ``country`` is outside the client's home jurisdiction."""
        if not self.country:
            return None
        if not home_country:
            return None
        party = self.country.strip().upper()
        home = home_country.strip().upper()
        home_codes = _HOME_COUNTRY_EQUIVALENTS.get(home, {home})
        party_codes = _HOME_COUNTRY_EQUIVALENTS.get(party, {party})
        if party_codes & home_codes:
            return False
        return True


@dataclass
class InvoiceLine:
    """One line item / charge. After classification, `tax_treatment` is set."""

    description: str
    quantity: Optional[float] = None
    unit_amount: Optional[float] = None    # unit price, ex-tax
    net_amount: Optional[float] = None     # line amount, ex-tax
    gst_amount: Optional[float] = None     # GST on this line (0 for ZR/ES/OS)
    account_code: Optional[str] = None     # COA code from categorization (later phase)
    account_flagged: bool = False          # low-confidence / unresolved COA pick (WS-3.4)
    account_flag_reason: Optional[str] = None  # e.g. low_avg_logprobs, unresolved
    account_alternative_codes: list[str] = field(default_factory=list)  # LLM runner-ups (WS-3.5)
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
    currency: str = ""
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

    # FX / multi-currency fields (set by to_normalized).
    # fx_rate: the exchange rate PRINTED on the document, recorded as-is.
    #   - None when the document prints no rate (booked in document currency as-is).
    #   - Never silently set to 1.0 — the accountant converts in their ERP.
    # original_currency / original_total: always None (no conversion is performed).
    # needs_fx_review: True only when a single document mixes multiple currencies.
    fx_rate: Optional[float] = None
    original_total: Optional[float] = None
    original_currency: Optional[str] = None
    needs_fx_review: bool = False

    # Reconciliation of the ledger lines against the document totals (set in to_normalized).
    reconciled: bool = True
    reconcile_note: Optional[str] = None

    # Set by capture/book when no Tax/GST column exists — drives classifier + export.
    tax_visible_on_document: Optional[bool] = None
    direction_reason: Optional[str] = None

    # The classify document kind — e.g. "credit_note", "invoice", "receipt",
    # "statement_of_account", "expense_claim", ...
    # Distinct from `doc_type` above, which is the DIRECTION ("purchase"/"sales").
    # Populated from state[DOC_TYPE_KEY] after extraction and used by exporters
    # to apply the credit-note sign-flip (negative amounts on export).
    document_kind: Optional[str] = None  # classify doc_type: invoice/credit_note/receipt/... (NOT direction)

    # Provenance from extraction (WS-5.4): page span within the source PDF and
    # the Slack file id for this processing run (not used in dedupe keys).
    page_range: Optional[tuple[int, int]] = None
    source_file_id: Optional[str] = None

    # Stable per-logical-document id used to tag exported rows so a golden
    # scorer can join live output rows back to expected per-line values
    # (issue #28). Derived from source basename + reference + page_range via
    # invoice_processing.export.source_doc_id; survives multi-doc fan-out and is
    # reproducible run-to-run (unlike source_file_id, which rotates). Stamped
    # onto each exporter row dict but never written to the human-facing Excel.
    source_doc_id: Optional[str] = None

    # Faithful tax breakdown captured by extraction (fix 1d, Phase-1 port).
    # Previously the extracted ``tax_lines[]`` field was discarded downstream;
    # this list preserves what the bill itself prints so reconciliation,
    # exporters, and reviewers can see the same SR / ZR / exempt split that
    # Drive's Gemini sidebar shows. Each entry is the verbatim (label, rate,
    # base, amount) tuple. Populated by
    # ``extracted_document_to_normalized``; not used by the canonical tax
    # classifier (which still drives per-line SR/ZR/ES labels) — purely an
    # audit / Drive-parity surface.
    tax_breakdown: list[dict] = field(default_factory=list)

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
    currency: str = ""
    statement_period: Optional[str] = None
    opening_balance: Optional[float] = None
    closing_balance: Optional[float] = None
    transactions: list[BankTransaction] = field(default_factory=list)
    source_file_id: Optional[str] = None
    extract_mode: Optional[str] = None   # "digital" (pdfplumber) or "vision"
    reconciled: bool = True
    reconcile_note: Optional[str] = None
