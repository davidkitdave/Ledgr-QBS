"""Phase 1 document extraction — faithful field capture via Gemini structured output."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from google.genai import types

from ..shared_libraries.gemini_call_config import default_llm_config
from ..shared_libraries.genai_client import lite_model, make_client
from .document_record import DocumentRecordBundle
from .invoice_extractor import _append_hint, mime_for

logger = logging.getLogger(__name__)

PHASE1_PROMPT = """You are extracting ALL visible information from a financial document
into a structured capture. Your job is to READ faithfully — not to summarize for bookkeeping.

Rules:
- Populate ``labeled_fields`` with every labeled key-value pair you can see (Invoice Date,
  Invoice Number, Subject, Contact, Date Range, Job Number, etc.).
- If the sender appears only on letterhead/logo with no "From:" label, add a labeled field
  with label "From" and source inferred_letterhead.
- Capture ``parties`` with role_hint (letterhead, to_block, sender_block, employee, …).
- Capture ``line_items`` verbatim — one entry per visible row; do NOT collapse rows into
  ledger summary lines.
- Capture ``totals`` (Sub Total, GST, Tax, Total, …) as labeled_fields in the totals list.
- Capture stamps, handwritten notes, and red overlays in ``annotations`` (kind=payment_stamp
  for paid stamps).
- For grid/table forms (expense claims), also capture ``tables`` with row arrays.
- Do NOT decide purchase/sales, account codes, GST treatment, or FX policy.
- Do NOT invent values; leave optional fields empty when not visible.

Segmentation:
- If the file contains MULTIPLE distinct documents, return one ``documents`` entry each.
- For SOA packages: skip the summary/cover page (record page numbers in skipped_pages) and
  extract only embedded invoices.
- If the file is a single document, return a one-element documents list.

Segmentation (expense packages):
- If multiple pages belong to ONE expense claim (form + receipt attachments),
  return ONE ``documents`` entry with combined ``tables[]`` and ``line_items[]``.
- Only split into multiple ``documents`` when you see distinct invoice numbers
  each with its own separate total on different pages.
- Supporting receipt pages without their own invoice number are NOT separate documents.
"""


def extract_document_bundle(
    data: bytes,
    mime_type: str,
    *,
    project: Optional[str] = None,
    location: Optional[str] = None,
    model: Optional[str] = None,
    hint: Optional[str] = None,
    use_layout_parser: bool = False,
    phase1_prompt: Optional[str] = None,
) -> DocumentRecordBundle:
    """Extract faithful DocumentRecord(s) from a PDF or image."""
    if use_layout_parser:
        from .layout_parser import maybe_preprocess_tables

        preprocessed = maybe_preprocess_tables(data, mime_type)
        if preprocessed is not None:
            data, mime_type = preprocessed

    client = make_client(project, location)
    model = model or lite_model()
    prompt = phase1_prompt if phase1_prompt is not None else PHASE1_PROMPT
    part = types.Part.from_bytes(data=data, mime_type=mime_type)
    resp = client.models.generate_content(
        model=model,
        contents=[part, _append_hint(prompt, hint)],
        config=default_llm_config(
            temperature=0,
            response_mime_type="application/json",
            response_schema=DocumentRecordBundle,
        ),
    )
    return DocumentRecordBundle.model_validate_json(resp.text)


def extract_document_file(path: str | Path, **kwargs) -> DocumentRecordBundle:
    path = Path(path)
    return extract_document_bundle(path.read_bytes(), mime_for(path), **kwargs)
