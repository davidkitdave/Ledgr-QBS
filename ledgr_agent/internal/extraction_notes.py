"""Read-only extraction annotations — notes only, no line repair (ADR-0034 / ADR-0035)."""

from __future__ import annotations

from typing import Any

_OVER_EXTRACTION_LINE_THRESHOLD = 8


def annotate_over_extraction_notes(bundle: dict[str, Any]) -> dict[str, Any]:
    """Set ``notes`` when line count suggests appendix over-copy; never mutate ``lines``."""
    if bundle.get("file_kind") != "commercial_documents":
        return bundle
    documents = bundle.get("documents") or []
    if not documents:
        return bundle

    updated_docs: list[dict[str, Any]] = []
    changed = False
    for doc in documents:
        if not isinstance(doc, dict):
            updated_docs.append(doc)
            continue
        lines = doc.get("lines") or []
        line_grain = str(doc.get("line_grain") or "itemized").strip().lower()
        if line_grain == "itemized" or len(lines) <= _OVER_EXTRACTION_LINE_THRESHOLD:
            updated_docs.append(doc)
            continue
        note = (
            "Extraction returned many summary-level lines; verify against printed "
            "charge summary or tax breakdown."
        )
        existing = str(doc.get("notes") or "").strip()
        merged = note if not existing else f"{existing}; {note}"
        if merged != existing:
            changed = True
            updated_docs.append({**doc, "notes": merged})
        else:
            updated_docs.append(doc)

    if not changed:
        return bundle
    return {**bundle, "documents": updated_docs}
