"""Multi-document bundle schema for the light batch path."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ledgr_agent.agents.document_reader import ReadDocument
from ledgr_agent.models.bank_statement import BankAccount

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


__all__ = [
    "BUNDLE_READER_INSTRUCTION",
    "FileKind",
    "ReadDocument",
    "ReadDocumentBundle",
]
