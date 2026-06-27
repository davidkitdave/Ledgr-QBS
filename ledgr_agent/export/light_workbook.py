"""Build workbook bytes from light-path sheet dicts."""

from __future__ import annotations

from io import BytesIO
from typing import Any


def build_workbook_bytes(sheets: list[dict[str, Any]]) -> bytes:
    """Serialize sheet dicts ``{title, columns, rows}`` into one XLSX file."""
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.remove(workbook.active)
    for sheet in sheets:
        title = str(sheet.get("title") or "Sheet")[:31]
        ws = workbook.create_sheet(title=title)
        columns = list(sheet.get("columns") or [])
        if columns:
            ws.append(columns)
        for row in sheet.get("rows") or []:
            if isinstance(row, dict):
                ws.append([row.get(col, "") for col in columns])
    buf = BytesIO()
    workbook.save(buf)
    return buf.getvalue()
