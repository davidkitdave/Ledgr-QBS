"""Gemini-multimodal document classifier.

Given a document (PDF or image bytes), classify its type and read the issuer / bill-to
parties + currency + total. Purchase-vs-sales is *direction*, resolved separately from the
client's identity (the client owns the Slack channel): the client as bill-to => purchase,
the client as issuer => sales.

Runs on Gemini Flash in-region (asia-southeast1), so it handles scanned PDFs and phone-photo
receipts without Document AI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from google.genai import types
from pydantic import BaseModel, Field

from ..shared_libraries.genai_client import default_model, make_client

ALLOWED_DOC_TYPES = [
    "invoice",              # tax invoice / bill / proforma / telco bill (goods or services)
    "receipt",              # payment receipt / payment confirmation / cashbook transaction slip
    "bank_statement",       # bank account statement listing transactions
    "credit_note",          # credit note / refund
    "statement_of_account", # SOA listing multiple invoices / balances
    "other",
]

_MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class ClassificationResult(BaseModel):
    doc_type: str = Field(description="One of: " + ", ".join(ALLOWED_DOC_TYPES))
    issuer_name: Optional[str] = Field(None, description="Party that issued/sent the document")
    bill_to_name: Optional[str] = Field(None, description="Party the document is addressed/billed to")
    currency: Optional[str] = Field(None, description="ISO currency code if visible, e.g. SGD/MYR/USD")
    total_amount: Optional[float] = Field(None, description="Document grand total if visible")
    confidence: float = Field(description="0.0-1.0 confidence in doc_type")
    reason: str = Field(description="One-line justification")


_PROMPT = """You classify financial documents for a Singapore/Malaysia bookkeeping system.

Classify the attached document into exactly ONE doc_type:
- invoice: a tax invoice or bill for goods/services (includes proforma invoices, service
  invoices, and telecom/utility bills).
- receipt: a payment receipt, payment confirmation, or cashbook transaction slip.
- bank_statement: a bank account statement listing dated transactions and balances.
- credit_note: a credit note or refund document.
- statement_of_account: a statement listing multiple invoices/balances (an SOA), not a single bill.
- other: anything that does not fit the above.

Also read:
- issuer_name: the business that issued/sent the document (the "From" / letterhead party).
- bill_to_name: the party it is addressed/billed to (the "Bill To" / "To" party).
- currency (ISO code) and total_amount if clearly visible.

Return confidence (0..1) and a one-line reason. Be precise: a telco bill is an invoice; a
DBS/OCBC/UOB account statement is a bank_statement; a payment slip is a receipt."""


def mime_for(path: str | Path) -> str:
    return _MIME_BY_EXT.get(Path(path).suffix.lower(), "application/octet-stream")


def classify_document(
    data: bytes,
    mime_type: str,
    *,
    project: Optional[str] = None,
    location: Optional[str] = None,
    model: Optional[str] = None,
) -> ClassificationResult:
    """Classify a single document (PDF or image bytes)."""
    client = make_client(project, location)
    model = model or default_model()
    part = types.Part.from_bytes(data=data, mime_type=mime_type)
    resp = client.models.generate_content(
        model=model,
        contents=[part, _PROMPT],
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=ClassificationResult,
        ),
    )
    result = ClassificationResult.model_validate_json(resp.text)
    if result.doc_type not in ALLOWED_DOC_TYPES:
        result.doc_type = "other"
    return result


def classify_file(path: str | Path, **kwargs) -> ClassificationResult:
    path = Path(path)
    return classify_document(path.read_bytes(), mime_for(path), **kwargs)


def _norm(s: Optional[str]) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def resolve_direction(
    result: ClassificationResult,
    client_name: Optional[str] = None,
    client_uen: Optional[str] = None,
) -> str:
    """Resolve purchase vs sales using the client's identity.

    Returns 'purchase' (client is the bill-to), 'sales' (client is the issuer),
    or 'unknown' if it can't be determined. Only meaningful for invoice/credit_note.
    """
    if result.doc_type not in ("invoice", "credit_note", "statement_of_account"):
        return "n/a"
    if not client_name:
        return "unknown"
    c = _norm(client_name)
    issuer = _norm(result.issuer_name)
    billed = _norm(result.bill_to_name)
    client_is_issuer = bool(c) and (c in issuer or issuer in c) and len(issuer) > 3
    client_is_billed = bool(c) and (c in billed or billed in c) and len(billed) > 3
    if client_is_billed and not client_is_issuer:
        return "purchase"
    if client_is_issuer and not client_is_billed:
        return "sales"
    return "unknown"
