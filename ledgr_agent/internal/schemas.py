"""Pydantic schemas for read_doc and billing."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

READER_INSTRUCTION = """You read one attached financial document (invoice, tax invoice,
receipt, credit note, bill, or similar) and return exactly one JSON object
matching the output schema.

Read only what is printed. Do not infer tax codes, account codes, or COA.

doc_type rule (very important):
- "purchase" when the document is a supplier bill we are paying — i.e. the
  Bill To / "Billed to" / "Customer" party is us, and the issuer is a vendor.
  Heading "TAX INVOICE" alone does NOT mean sales.
- "sales" when the document is an invoice we issue to our customer.
When the layout is ambiguous, prefer the issuer-vs-Bill-To check over the
title on the page.

document_kind is the document label printed on the page
(invoice, credit_note, receipt, etc.).

Dates: ISO (YYYY-MM-DD) when visible. If the printed date is in another
format, normalise to ISO in the output.

Line items:
- One entry per printed charge row, in printed order.
- net_amount, tax_amount, total_amount must be the printed amounts for that
  row, in the document's currency. Never leave them null when a line total
  is visible on the page.
- quantity and unit_amount from the printed row when shown.

Header totals:
- subtotal = sum of printed line net_amounts (or the printed subtotal if shown).
- tax_total = sum of printed line tax_amounts (or the printed tax_total).
- grand_total = printed grand total / amount due.

Reconcile before returning: lines sum should match subtotal; subtotal +
tax_total should match grand_total. If a printed value conflicts with the
sum, prefer the printed header value but list the mismatch in notes.

notes: brief extraction caveats only (empty string if none).

If multiple logical documents appear in one file, return the primary bill
only. Do not split a single bill into multiple documents.
"""


class Line(BaseModel):
    description: str = Field(
        default="",
        description="Printed line description as it appears on the document.",
    )
    quantity: float | None = Field(
        default=None,
        description="Printed quantity for this line. 1.0 when a service line has no quantity column.",
    )
    unit_amount: float | None = Field(
        default=None,
        description="Printed unit/net price per unit (before tax) for this line.",
    )
    net_amount: float | None = Field(
        default=None,
        description=(
            "Printed line subtotal — net of tax. Required when the line shows a total. "
            "Must equal quantity * unit_amount for unit-priced lines."
        ),
    )
    tax_amount: float | None = Field(
        default=None,
        description="Printed tax (GST/VAT) for this line. 0 for zero-rated or exempt lines.",
    )
    total_amount: float | None = Field(
        default=None,
        description="Printed line grand total (net + tax) for this line.",
    )


class ReadDocument(BaseModel):
    doc_type: str = Field(
        default="purchase",
        description=(
            "'purchase' for supplier bills we pay (we are Bill To). "
            "'sales' for invoices we issue to our customer. "
            "Decide from the issuer vs Bill-To layout, not from the document title."
        ),
    )
    document_kind: str = Field(
        default="invoice",
        description="Printed document label: 'invoice', 'credit_note', 'receipt', etc.",
    )
    vendor_name: str = Field(
        default="",
        description="Name of the issuer / supplier (the party sending the bill).",
    )
    customer_name: str = Field(
        default="",
        description="Name of the bill-to / customer (the party being asked to pay).",
    )
    entity_tax_id: str = Field(
        default="",
        description="GST/VAT/tax registration number of the issuer if printed.",
    )
    invoice_number: str = Field(default="", description="Printed invoice / bill number.")
    invoice_date: str = Field(default="", description="Printed invoice date, normalised to ISO YYYY-MM-DD.")
    due_date: str = Field(default="", description="Printed due date, normalised to ISO YYYY-MM-DD.")
    currency: str = Field(default="", description="Three-letter currency code printed on the document (SGD, USD, ...).")
    fx_rate: float | None = Field(default=None, description="Exchange rate to base currency if printed, else null.")
    subtotal: float | None = Field(
        default=None,
        description="Sum of line net_amounts (or the printed subtotal). Required when lines exist.",
    )
    tax_total: float | None = Field(
        default=None,
        description="Sum of line tax_amounts (or the printed tax_total). Required when tax lines exist.",
    )
    grand_total: float | None = Field(
        default=None,
        description="Printed grand total / amount due. Must equal subtotal + tax_total.",
    )
    lines: list[Line] = Field(
        default_factory=list,
        description="Printed line items in printed order. Empty list only when the document is a header-only memo.",
    )
    notes: str = Field(default="", description="Short extraction caveats; empty string when clean.")


class BankTxn(BaseModel):
    date: str | None = Field(
        default=None,
        description="ISO date YYYY-MM-DD when visible on the statement row.",
    )
    description: str = Field(default="", description="Transaction narrative as printed.")
    bank_ref: str | None = Field(default=None, description="Cheque or bank reference if shown.")
    withdrawal: float | None = Field(default=None, description="Debit / paid out (positive).")
    deposit: float | None = Field(default=None, description="Credit / received (positive).")
    balance: float | None = Field(default=None, description="Running balance after this row.")


class BankAccount(BaseModel):
    bank_name: str = Field(
        default="",
        description=(
            "Bank label only (e.g. 'OCBC' or 'DBS Bank Ltd'). "
            "Do NOT embed account digits in this field."
        ),
    )
    account_number: str | None = Field(default=None, description="Account number as printed.")
    currency: str | None = Field(default=None, description="ISO currency code (default SGD).")
    statement_period: str | None = Field(
        default=None,
        description="Printed period, e.g. '01 DEC 2024 - 31 DEC 2024'.",
    )
    opening_balance: float | None = Field(
        default=None,
        description="Brought-forward / opening balance (not a transaction row).",
    )
    closing_balance: float | None = Field(default=None, description="Final balance on the statement.")
    transactions: list[BankTxn] = Field(default_factory=list)


class ReadBankStatement(BaseModel):
    accounts: list[BankAccount] = Field(
        default_factory=list,
        description="One entry per distinct (account_number, currency).",
    )


BUNDLE_READER_INSTRUCTION = """You read one attached financial file. First decide ``file_kind``:

- ``bank_statement`` — bank / account statement with transaction rows, balances, debits/credits.
- ``commercial_documents`` — invoices, tax invoices, receipts, credit notes, bills, SOA packs,
  or multi-receipt PDFs.

When ``file_kind`` is ``bank_statement``:
- Fill ``accounts`` with every account section (one entry per distinct account_number + currency).
- Leave ``documents`` empty and ``document_count`` 0.
- Extract EVERY transaction row in order; opening_balance is not a transaction row.
- bank_name = bank label only (e.g. 'OCBC'), not account digits in the name.

When ``file_kind`` is ``commercial_documents``:
- Fill ``documents`` with one entry per distinct logical bill / invoice / receipt / credit note.
- Leave ``accounts`` empty.
- Read ALL pages. Skip pure SOA summary pages that only list invoice numbers without bill detail.
- Do NOT split one invoice into multiple documents or merge separate invoices.

Per commercial document:
- doc_type: "purchase" when we are Bill To; "sales" when we issue to our customer.
- document_kind: printed label (invoice, credit_note, receipt, ...).
- Dates in ISO YYYY-MM-DD when visible.
- One line item per printed charge row; net_amount, tax_amount, total_amount from the page.
- Reconcile lines to subtotal / tax_total / grand_total; note mismatches in notes.

notes on each document: brief extraction caveats only (empty string if none).
"""

FileKind = Literal["bank_statement", "commercial_documents"]


class ReadDocumentBundle(BaseModel):
    file_kind: FileKind = Field(
        description=(
            "bank_statement when the file is a bank/account statement; "
            "commercial_documents for invoices, receipts, bills, or SOA packs."
        ),
    )
    accounts: list[BankAccount] = Field(
        default_factory=list,
        description="Populated when file_kind is bank_statement; empty otherwise.",
    )
    documents: list[ReadDocument] = Field(
        default_factory=list,
        description=(
            "One entry per logical commercial document when file_kind is commercial_documents. "
            "Empty for bank statements."
        ),
    )
    document_count: int = Field(
        default=0,
        description="Number of commercial documents (must equal len(documents)).",
    )


CreditStatus = Literal["not_checked", "estimated", "charged", "blocked", "not_billable"]


class CreditSummary(BaseModel):
    """Billing summary attached to every batch result."""

    credits_estimated: int = 0
    credits_used: int = 0
    credits_remaining: int | None = None
    credit_status: CreditStatus = "not_checked"
    credit_ledger_refs: list[str] = Field(default_factory=list)
