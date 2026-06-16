"""Deterministic merge of multi-page expense packages into one logical DocumentRecord.

Pattern-agnostic: uses structure signals (shared employee, claim reference, no
distinct invoice totals) — not vendor or doc_type names.
"""

from __future__ import annotations

import re
from typing import Optional

from .document_record import (
    AnnotationCapture,
    DocumentRecord,
    DocumentRecordBundle,
    LabeledField,
    LineCapture,
    PartyCapture,
    TableCapture,
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _claim_reference(record: DocumentRecord) -> Optional[str]:
    for f in record.labeled_fields:
        combined = f"{f.label} {f.value}"
        m = re.search(r"AAI-\d{2}-\d{3}", combined, re.I)
        if m:
            return m.group(0).upper()
        if "claim" in _norm(f.label) and f.value.strip():
            return f.value.strip()[:80]
    for f in record.totals:
        m = re.search(r"AAI-\d{2}-\d{3}", f.value or "", re.I)
        if m:
            return m.group(0).upper()
    return None


def _employee_name(record: DocumentRecord) -> Optional[str]:
    for p in record.parties:
        if p.role_hint.lower() in ("employee", "claimant"):
            return _norm(p.name)
    for f in record.labeled_fields:
        if "employee" in _norm(f.label) or "name" in _norm(f.label):
            if f.value.strip():
                return _norm(f.value)
    return None


def _distinct_invoice_numbers(docs: list[DocumentRecord]) -> set[str]:
    nums: set[str] = set()
    inv_pat = re.compile(r"invoice\s*(?:#|no\.?|number)?\s*[:.]?\s*(\S+)", re.I)
    for doc in docs:
        for f in doc.labeled_fields + doc.totals:
            nl = _norm(f.label)
            m = inv_pat.search(f"{f.label} {f.value}")
            if m:
                nums.add(m.group(1).strip().upper())
            elif re.match(r"^(IA|CNA)-\d+$", (f.value or "").strip(), re.I):
                nums.add(f.value.strip().upper())
            elif nl in ("no.", "no") and (f.value or "").strip():
                nums.add(f.value.strip().upper())
            elif re.match(r"^\d{2}-D\d{2}$", (f.value or "").strip(), re.I):
                nums.add(f.value.strip().upper())
    return nums


def should_merge_package(docs: list[DocumentRecord]) -> bool:
    """True when multiple captures likely belong to one expense/claim package."""
    if len(docs) <= 1:
        return False

    distinct_inv = _distinct_invoice_numbers(docs)
    if len(distinct_inv) >= 2:
        return False

    employees = {_employee_name(d) for d in docs}
    employees.discard(None)
    if len(employees) == 1:
        return True

    refs = {_claim_reference(d) for d in docs}
    refs.discard(None)
    if refs and len(refs) == 1:
        return True

    kinds = {_norm(d.doc_kind_guess or "") for d in docs}
    if "expense" in " ".join(kinds) or any("claim" in k for k in kinds):
        return True

    # Multiple thin captures (receipt-only pages) with no distinct invoice numbers.
    if len(docs) >= 2 and len(distinct_inv) <= 1:
        thin = sum(1 for d in docs if len(d.line_items) <= 2 and not d.labeled_fields)
        if thin >= len(docs) - 1:
            return True

    return False


def merge_document_records(bundle: DocumentRecordBundle) -> DocumentRecordBundle:
    """Collapse a multi-capture bundle into one record when heuristics match."""
    docs = list(bundle.documents)
    if not should_merge_package(docs):
        return bundle

    primary = docs[0].model_copy(deep=True)
    for extra in docs[1:]:
        primary.labeled_fields.extend(extra.labeled_fields)
        primary.parties.extend(extra.parties)
        primary.line_items.extend(extra.line_items)
        primary.totals.extend(extra.totals)
        primary.annotations.extend(extra.annotations)
        primary.tables.extend(extra.tables)
        if extra.notes:
            primary.notes = (
                f"{primary.notes}; {extra.notes}" if primary.notes else extra.notes
            )

    # Dedupe parties by name+role
    seen: set[tuple[str, str]] = set()
    unique_parties: list[PartyCapture] = []
    for p in primary.parties:
        key = (_norm(p.name), p.role_hint.lower())
        if key not in seen:
            seen.add(key)
            unique_parties.append(p)
    primary.parties = unique_parties

    return DocumentRecordBundle(
        documents=[primary],
        skipped_pages=bundle.skipped_pages,
        notes=(bundle.notes or "") + " [merged package]",
    )
