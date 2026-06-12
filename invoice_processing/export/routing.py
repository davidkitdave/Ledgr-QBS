"""Document routing — archive path + workbook destination (spec §4).

Given a processed document's type, direction, date, and the client's FYE month,
``route_document`` returns a ``DocRoute`` describing exactly where to archive the
source PDF in GCS and which workbook/sheet should receive the extracted rows.

Reference: docs/superpowers/specs/2026-06-12-ledgr-client-onboarding-fy-routing-design.md §4
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from .fy import fy_for_date


@dataclass(frozen=True)
class DocRoute:
    fy: int                 # financial-year label
    bucket: str             # GCS subdir under the FY: "purchase" | "sales" | "bank"
    archive_path: str       # "{client_id}/FY{fy}/{bucket}/{filename}"  (object path, no gs:// bucket prefix)
    workbook: str           # "Ledger_FY{fy}.xlsx" | "BankStatement_FY{fy}.xlsx"
    sheet: Optional[str]    # "Purchase" | "Sales" | None (bank: per-account sheets handled by the exporter)


def route_document(
    *,
    doc_type: str,
    direction: Optional[str],
    doc_date: date,
    fye_month: int,
    client_id: str,
    filename: str,
) -> DocRoute:
    """Return the archive path and workbook destination for a processed document (spec §4)."""
    # Normalise inputs
    norm_type = (doc_type or "").strip().lower()
    norm_dir = (direction or "").strip().lower()

    fy = fy_for_date(doc_date, fye_month)

    if norm_type in ("bank_statement", "bank"):
        bucket = "bank"
        workbook = f"BankStatement_FY{fy}.xlsx"
        sheet = None
    elif norm_type == "receipt":
        # Receipts are always purchase-side
        bucket = "purchase"
        workbook = f"Ledger_FY{fy}.xlsx"
        sheet = "Purchase"
    else:
        # invoice (or any unrecognised type): use direction; default to purchase
        if norm_dir == "sales":
            bucket = "sales"
            sheet = "Sales"
        else:
            # direction == "purchase", None, or unknown → purchase
            bucket = "purchase"
            sheet = "Purchase"
        workbook = f"Ledger_FY{fy}.xlsx"

    archive_path = f"{client_id}/FY{fy}/{bucket}/{filename}"

    return DocRoute(
        fy=fy,
        bucket=bucket,
        archive_path=archive_path,
        workbook=workbook,
        sheet=sheet,
    )
