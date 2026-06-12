"""Invoice / receipt extraction via Gemini Flash (multimodal).

Turns a document (PDF or image bytes) into a structured `ExtractedInvoice`, then maps it to
the export-layer `NormalizedInvoice` given the resolved direction (purchase/sales). Each charge
line is kept separate so a mixed SR/ZR bill stays two lines for the tax classifier + exporter.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Optional

from google.genai import types
from pydantic import BaseModel, Field

from ..export.models import InvoiceLine, NormalizedInvoice, PartyInfo
from ..shared_libraries.genai_client import default_model, make_client

_MIME_BY_EXT = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
}


class ExtractedLine(BaseModel):
    description: str = Field(description="Line item / charge description")
    quantity: Optional[float] = None
    unit_amount: Optional[float] = Field(None, description="Unit price, excluding GST")
    net_amount: Optional[float] = Field(None, description="Line amount, excluding GST")
    gst_amount: Optional[float] = Field(None, description="GST on this line (0 if zero-rated/exempt)")
    tax_label: Optional[str] = Field(None, description="Any explicit tax wording on the line, e.g. SR, ZR, 9%, 0%, exempt")


class ExtractedInvoice(BaseModel):
    doc_type: str = Field(description="invoice or receipt")
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = Field(None, description="ISO date YYYY-MM-DD if determinable")
    due_date: Optional[str] = None
    currency: Optional[str] = Field(None, description="ISO currency code, e.g. SGD/MYR/USD")
    issuer_name: Optional[str] = Field(None, description="Supplier/seller — who issued the document")
    issuer_gst_regno: Optional[str] = Field(None, description="Issuer GST registration no. / UEN if shown")
    bill_to_name: Optional[str] = Field(None, description="Customer/buyer — who it is billed to")
    lines: list[ExtractedLine] = Field(default_factory=list)
    subtotal: Optional[float] = None
    gst_total: Optional[float] = None
    total: Optional[float] = None


_PROMPT = """You are extracting an invoice/receipt for a Singapore/Malaysia bookkeeping ledger.
Produce the SMALL set of summary lines a bookkeeper would post — NOT every itemized line.

How to choose the lines:
- PREFER the document's own summary/totals section. Telco, utility, and multi-page bills almost
  always have a 'Summary of Charges' / 'Bill Summary' / 'Account Summary' / 'Total Charges'
  section that breaks charges down by category and tax. Read THAT. Do NOT enumerate per-call,
  per-message, or per-phone-number itemized lines across pages.
- Split lines by GST tax treatment, because the ledger needs it: standard-rated (GST 9% / 'G')
  charges as one or more SR lines; zero-rated/international (0% / 'Z') charges as ZR line(s);
  exempt as ES. If the summary already separates GST-applicable vs zero-rated/international
  amounts, use that split.
- For a simple invoice/receipt with only a handful of distinct line items and no separate
  summary, keep those line items as-is (they're already ledger-appropriate).

Per line:
- description = the category/summary label, e.g. 'Telecommunication services - standard rated',
  'International roaming - zero rated'.
- net_amount = the ex-GST subtotal for that group.
- gst_amount = GST for that group (0 for ZR/ES).
- tax_label normalized to one of SR (standard/GST/9%/G), ZR (zero-rated/0%/Z/international),
  ES (exempt/E), OS (out-of-scope), or NT (no tax). If only a single-letter code (G/Z/E) is
  printed, map G→SR, Z→ZR, E→ES.
- Leave quantity and unit_amount null for summary lines.

Document-level fields:
- issuer_name = the supplier/seller (letterhead/"From"); bill_to_name = who it is addressed to.
- issuer_gst_regno = the supplier's GST registration number / UEN if printed.
- invoice_date in ISO YYYY-MM-DD if you can determine it; currency as ISO code.
- Always also return invoice-level subtotal, gst_total, total (the grand totals from the bill),
  used for reconciliation.

- Do not invent values; if the summary is unclear, return your best ledger-level grouping and
  ensure the line nets + GST reconcile to the document totals. Leave a field null if not visible."""


def mime_for(path: str | Path) -> str:
    return _MIME_BY_EXT.get(Path(path).suffix.lower(), "application/octet-stream")


def extract_invoice(
    data: bytes,
    mime_type: str,
    *,
    project: Optional[str] = None,
    location: Optional[str] = None,
    model: Optional[str] = None,
) -> ExtractedInvoice:
    client = make_client(project, location)
    model = model or default_model()
    part = types.Part.from_bytes(data=data, mime_type=mime_type)
    resp = client.models.generate_content(
        model=model,
        contents=[part, _PROMPT],
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=ExtractedInvoice,
        ),
    )
    return ExtractedInvoice.model_validate_json(resp.text)


def extract_file(path: str | Path, **kwargs) -> ExtractedInvoice:
    path = Path(path)
    return extract_invoice(path.read_bytes(), mime_for(path), **kwargs)


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def reconcile(ex: ExtractedInvoice, *, tol_abs: float = 0.05, tol_rel: float = 0.01) -> tuple[bool, str]:
    """Check that the ledger lines tie out to the document totals.

    Returns (ok, detail). ok=False means the summary grouping dropped/duplicated money and
    should be flagged for human review. Uses max(tol_abs, tol_rel*reference) tolerance.
    """
    net_sum = sum(l.net_amount or 0.0 for l in ex.lines)
    gst_sum = sum(l.gst_amount or 0.0 for l in ex.lines)

    mismatches: list[str] = []

    def _check(label: str, computed: float, reference: Optional[float]) -> None:
        if reference is None:
            return
        tol = max(tol_abs, tol_rel * abs(reference))
        if abs(computed - reference) > tol:
            mismatches.append(
                f"{label}: lines={computed:.2f} vs doc={reference:.2f} (diff={computed - reference:+.2f}, tol={tol:.2f})"
            )

    _check("subtotal", net_sum, ex.subtotal)
    _check("gst", gst_sum, ex.gst_total)
    _check("total", net_sum + gst_sum, ex.total)

    if mismatches:
        return False, "; ".join(mismatches)
    return True, "reconciled"


def to_normalized(
    ex: ExtractedInvoice,
    *,
    direction: str,
    our_gst_registered: bool = True,
    client_country: str = "SG",
) -> NormalizedInvoice:
    """Map an ExtractedInvoice to a NormalizedInvoice for the tax classifier + exporter.

    direction: 'purchase' (we are the buyer; counterparty = supplier=issuer) or
    'sales' (we are the seller; counterparty = customer=bill_to).
    """
    doc_type = "sales" if direction == "sales" else "purchase"
    supplier = PartyInfo(name=ex.issuer_name, gst_regno=ex.issuer_gst_regno)
    customer = PartyInfo(name=ex.bill_to_name)
    lines = [
        InvoiceLine(
            description=l.description,
            quantity=l.quantity,
            unit_amount=l.unit_amount,
            net_amount=l.net_amount,
            gst_amount=l.gst_amount,
            tax_keyword=l.tax_label,
        )
        for l in ex.lines
    ]
    ok, detail = reconcile(ex)
    return NormalizedInvoice(
        doc_type=doc_type,
        invoice_number=ex.invoice_number,
        invoice_date=_parse_date(ex.invoice_date),
        due_date=_parse_date(ex.due_date),
        currency=ex.currency or "SGD",
        supplier=supplier,
        customer=customer,
        lines=lines,
        our_gst_registered=our_gst_registered,
        reconciled=ok,
        reconcile_note=detail,
    )
