from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re


_INVOICE_BLOCK_RE = re.compile(
    r"(?ms)(?:^([A-Z]{2}-\d{5})\s+INVOICE\b|^INVOICE\s*:\s*([A-Z]{2}-\d{5})\b)"
    r"(?P<body>.*?)(?=^(?:[A-Z]{2}-\d{5}\s+INVOICE\b|INVOICE\s*:)|\Z)",
)
_DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
_TOTAL_RE = re.compile(r"Total\s*\(RM\)\s*([\d,]+\.\d{2})", re.IGNORECASE)


def pdf_text(path: Path) -> str:
    """Extract a digital PDF text layer for QA comparison."""

    if path.suffix.lower() != ".pdf":
        return ""

    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def expected_invoices_from_text(text: str) -> list[dict[str, object]]:
    """Best-effort independent invoice truth from visible PDF text.

    This intentionally uses deterministic text parsing, not the LLM extraction,
    so it can catch obvious misses like a multi-invoice SOA package where only
    one embedded invoice was exported.
    """

    invoices: list[dict[str, object]] = []
    for match in _INVOICE_BLOCK_RE.finditer(text):
        invoice_number = match.group(1) or match.group(2)
        body = match.group("body")
        date_match = _DATE_RE.search(body)
        total_match = _TOTAL_RE.search(body)
        total = _decimal_from_text(total_match.group(1)) if total_match else None
        invoices.append(
            {
                "invoice_number": invoice_number,
                "invoice_date": date_match.group(1) if date_match else None,
                "total": float(total) if total is not None else None,
            }
        )
    return invoices


def document_truth_report(paths: list[Path], export_rows: list[dict[str, object]]) -> dict[str, object]:
    """Compare exported rows against visible invoice refs/totals in source PDFs."""

    expected: list[dict[str, object]] = []
    unreadable: list[str] = []
    for path in paths:
        try:
            expected.extend(expected_invoices_from_text(pdf_text(path)))
        except Exception:
            unreadable.append(str(path))

    if not expected:
        return {
            "status": "not_available",
            "source": "pdf_text",
            "reason": "no invoice markers found in digital text",
            "unreadable_files": unreadable,
        }

    exported_by_invoice = _exported_amounts_by_invoice(export_rows)
    expected_by_invoice = {
        str(item["invoice_number"]): item for item in expected if item.get("invoice_number")
    }
    expected_numbers = set(expected_by_invoice)
    exported_numbers = set(exported_by_invoice)
    matched_numbers = expected_numbers & exported_numbers
    missing_numbers = sorted(expected_numbers - exported_numbers)
    unexpected_numbers = sorted(exported_numbers - expected_numbers)

    expected_total = sum(
        _to_decimal(item.get("total")) or Decimal("0") for item in expected_by_invoice.values()
    )
    exported_total = sum(exported_by_invoice.values(), Decimal("0"))
    amount_coverage = (
        float((exported_total / expected_total).quantize(Decimal("0.0001")))
        if expected_total
        else None
    )

    status = "pass" if not missing_numbers and not unexpected_numbers else "fail"
    return {
        "status": status,
        "source": "pdf_text",
        "expected_invoice_count": len(expected_by_invoice),
        "exported_invoice_count": len(exported_numbers),
        "matched_invoice_count": len(matched_numbers),
        "missing_invoice_numbers": missing_numbers,
        "unexpected_invoice_numbers": unexpected_numbers,
        "expected_total_amount": float(expected_total),
        "exported_total_amount": float(exported_total),
        "amount_coverage": amount_coverage,
        "expected_invoices": list(expected_by_invoice.values()),
        "unreadable_files": unreadable,
    }


def _exported_amounts_by_invoice(rows: list[dict[str, object]]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in rows:
        invoice_number = _first_string(row, ("Invoice Number", "DocNo", "SupplierInvoiceNo"))
        if not invoice_number:
            continue
        amount = _first_decimal(row, ("Source Amount", "Amount", "Total Amount", "Total"))
        if amount is not None:
            totals[invoice_number] += amount
    return dict(totals)


def _first_string(row: dict[str, object], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_decimal(row: dict[str, object], keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        value = row.get(key)
        converted = _to_decimal(value)
        if converted is not None:
            return converted
    return None


def _to_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _decimal_from_text(value: str) -> Decimal | None:
    return _to_decimal(value)
