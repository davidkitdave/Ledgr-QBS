"""Phase 1 capture schema — pattern-agnostic document read (Drive-like).

One generic shape for all uploads: labeled fields, parties with role hints,
line items, totals, annotations, and optional raw tables. No vendor-specific
field names and no accounting projection at read time.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class LabeledField(BaseModel):
    label: str = Field(description="Human-readable field label as printed or inferred")
    value: str = Field(description="Field value as printed on the document")
    page: int = Field(default=1, description="1-based page number")
    source: str = Field(
        default="explicit_label",
        description=(
            "explicit_label | inferred_letterhead | table_cell | stamp | "
            "inferred_block"
        ),
    )


class PartyCapture(BaseModel):
    name: str
    role_hint: str = Field(
        description=(
            "letterhead | to_block | from_block | sender_block | employee | "
            "bill_to | unknown"
        ),
    )
    address: Optional[str] = None
    email: Optional[str] = None


class LineCapture(BaseModel):
    description: str
    quantity: Optional[float] = None
    unit_amount: Optional[float] = None
    net_amount: Optional[float] = None
    currency: Optional[str] = None
    tax_label: Optional[str] = Field(
        None,
        description="Verbatim tax wording only, e.g. GST15%, N/A, SR — not a treatment decision",
    )


class AnnotationCapture(BaseModel):
    text: str
    kind: str = Field(
        description="payment_stamp | handwritten | note | overlay",
    )
    page: int = 1


class TableCapture(BaseModel):
    """Raw table grid from form-style documents."""

    name: Optional[str] = None
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)


class DocumentRecord(BaseModel):
    """Faithful capture of one logical document — no bookkeeping summarization."""

    doc_kind_guess: Optional[str] = Field(
        None,
        description="Soft hint only (invoice, receipt, expense form, …); not routing authority",
    )
    labeled_fields: list[LabeledField] = Field(default_factory=list)
    parties: list[PartyCapture] = Field(default_factory=list)
    line_items: list[LineCapture] = Field(default_factory=list)
    totals: list[LabeledField] = Field(default_factory=list)
    annotations: list[AnnotationCapture] = Field(default_factory=list)
    tables: list[TableCapture] = Field(
        default_factory=list,
        description="Optional raw table captures for grid-style forms",
    )
    notes: Optional[str] = None


class DocumentRecordBundle(BaseModel):
    """One upload may contain several logical documents."""

    documents: list[DocumentRecord] = Field(
        default_factory=list,
        description="One entry per unique logical document in the upload",
    )
    skipped_pages: Optional[list[int]] = Field(
        None,
        description="1-based pages deliberately skipped (e.g. SOA cover)",
    )
    notes: Optional[str] = None
