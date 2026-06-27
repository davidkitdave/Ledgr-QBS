"""Document reader schema and instruction for the light Ledgr path.

This module is the canonical ERP-agnostic shape of one commercial document
(invoice / tax invoice / receipt / credit note). A single ``read_document``
``FunctionTool`` call turns the attached PDF into a :class:`ReadDocument`
JSON via Gemini's structured output, using :data:`READER_INSTRUCTION` plus
the Pydantic ``Field`` descriptions as the model's contract.

There is intentionally **no ADK ``Agent`` builder here** — extraction goes
through ``read_document_tool.read_document`` so the LLM call can carry the
real PDF bytes (an ``AgentTool`` only forwards text, not attachments).
"""

from __future__ import annotations

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
        description="Printed line description as it appears on the document (e.g. 'Audit for ISO 9001').",
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


__all__ = ["Line", "ReadDocument", "READER_INSTRUCTION"]
