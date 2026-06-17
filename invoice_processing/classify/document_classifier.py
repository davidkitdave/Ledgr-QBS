"""Gemini-multimodal document classifier.

Given a document (PDF or image bytes), classify its type and read the issuer / bill-to
parties + currency + total. Purchase-vs-sales is *direction*, resolved separately from the
client's identity (the client owns the Slack channel): the client as bill-to => purchase,
the client as issuer => sales.

Runs on Gemini Flash in-region (asia-southeast1), so it handles scanned PDFs and phone-photo
receipts without Document AI.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Optional

from google.genai import types
from pydantic import BaseModel, Field

from ..shared_libraries.genai_client import lite_model, make_client

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
    model = model or lite_model()
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


# Normalised name length must exceed this to participate in any match (exact or
# fuzzy).  Prevents single-word / initialism false-positives (preserves original
# len > 3 guard).
_MIN_NORM_LEN = 3

# Weighted best-token-match threshold: average of per-client-token best
# character-ratio scores must be at or above this value.
# 0.7 cleanly separates:
#   "sanesea international" vs "sanersea international"  -> 0.967  (typo)
#   "sanesea international" vs "sanesee international"   -> 0.929  (1-char swap)
#   "sanesea international" vs "sanesea intl"            -> 0.735  (abbreviation)
#   "sanesea international" vs "alpha corp"              -> 0.278  (unrelated)
_BEST_TOKEN_THRESHOLD = 0.7


def _normalise_party(s: Optional[str]) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for name comparison."""
    if not s:
        return ""
    t = s.lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _best_token_match(client_norm: str, party_norm: str) -> float:
    """Weighted best-token match score, 0.0–1.0.

    For each token in *client_norm*, find the highest character-level
    SequenceMatcher ratio across all tokens in *party_norm*, then average.
    This handles:
    - Single-character typos ("sanersea" -> "sanesea": ratio 0.933).
    - Abbreviations ("intl" best-matches "international": ratio 0.471, but
      "sanesea" exact-matches "sanesea": ratio 1.0, averaging above 0.7).
    - Word-order is irrelevant (each client token scans all party tokens).
    """
    c_toks = client_norm.split()
    p_toks = party_norm.split()
    if not c_toks or not p_toks:
        return 0.0
    total = 0.0
    for ct in c_toks:
        best = max(
            difflib.SequenceMatcher(None, ct, pt).ratio() for pt in p_toks
        )
        total += best
    return total / len(c_toks)


def _uen_norm(s: Optional[str]) -> str:
    """Strip spaces and uppercase a UEN/registration number for exact comparison."""
    return (s or "").replace(" ", "").upper()


def _party_matches_client(
    party_raw: Optional[str],
    client_norm: str,
    client_uen_norm: str,
) -> bool:
    """Return True if *party_raw* refers to the client.

    Resolution order:
    1. UEN exact match (if client_uen_norm is non-empty): scan *party_raw* for
       the UEN token.  Reliable even when the company name is garbled.
    2. Exact normalised-alphanumeric substring check (fast path, original behaviour).
    3. Weighted best-token character-ratio >= _BEST_TOKEN_THRESHOLD — catches
       single-character typos and abbreviations missed by substring check.

    All name paths require normalised len > _MIN_NORM_LEN (preserves the
    original len > 3 guard against short-string false positives).
    """
    if not party_raw:
        return False

    # --- 1. UEN path (preferred when available) ---
    if client_uen_norm:
        party_uen = _uen_norm(party_raw)
        if client_uen_norm in party_uen:
            return True

    # --- 2 & 3. Name paths ---
    party_norm = _normalise_party(party_raw)
    if len(party_norm) <= _MIN_NORM_LEN or len(client_norm) <= _MIN_NORM_LEN:
        return False

    # Fast path: exact alphanumeric substring (original behaviour).
    c_alpha = _norm(client_norm)
    p_alpha = _norm(party_raw)
    if c_alpha and p_alpha and (c_alpha in p_alpha or p_alpha in c_alpha):
        return True

    # Fuzzy path: weighted best-token character-ratio.
    return _best_token_match(client_norm, party_norm) >= _BEST_TOKEN_THRESHOLD


def resolve_direction(
    result: ClassificationResult,
    client_name: Optional[str] = None,
    client_uen: Optional[str] = None,
) -> str:
    """Resolve purchase vs sales using the client's identity.

    Returns:
    - ``'purchase'``: client is the bill-to party.
    - ``'sales'``: client is the issuer.
    - ``'self_referential'``: client appears as BOTH issuer AND bill-to — the
      document is self-referential (dividend cert, internal transfer, etc.) and
      must NEVER be booked as a purchase with the client as its own vendor.
    - ``'unknown'``: direction cannot be determined.
    - ``'n/a'``: doc_type is not one that has a direction (e.g. bank_statement).

    Resolution order inside the function:
    1. ``n/a`` guard — non-invoice doc types exit immediately.
    2. UEN match (preferred when ``client_uen`` is supplied): find the UEN
       token in issuer_name / bill_to_name text; exact, robust to name typos.
    3. Fuzzy name match with token-set ratio >= 0.5 (tolerates typos,
       abbreviations); inherits the original ``len > 3`` guard.
    4. Self-referential guard: if the client matches BOTH sides, return
       ``'self_referential'`` rather than a spurious direction.
    """
    if result.doc_type not in ("invoice", "credit_note", "statement_of_account"):
        return "n/a"
    if not client_name:
        return "unknown"

    client_norm = _normalise_party(client_name)
    client_uen_norm = _uen_norm(client_uen)

    client_is_issuer = _party_matches_client(result.issuer_name, client_norm, client_uen_norm)
    client_is_billed = _party_matches_client(result.bill_to_name, client_norm, client_uen_norm)

    # Self-referential guard: client on both sides -> neither purchase nor sales.
    if client_is_billed and client_is_issuer:
        return "self_referential"

    if client_is_billed:
        return "purchase"
    if client_is_issuer:
        return "sales"
    return "unknown"
