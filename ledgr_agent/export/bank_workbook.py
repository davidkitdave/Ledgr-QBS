"""Light-path bank workbook projection."""

from __future__ import annotations

from typing import Any

from ledgr_agent.normalize.bank_statement import _fmt_date, _num, normalize_bank_statement

BANK_COLS = [
    "Date",
    "Description",
    "Withdrawal",
    "Deposit",
    "Balance",
    "Currency",
    "Math_Check",
]

OPENING_MARKER = "BALANCE B/F"
TOTALS_MARKER = "TOTALS"


def _account_rows(account: dict[str, Any]) -> list[dict[str, Any]]:
    currency = account.get("currency") or ""
    rows: list[dict[str, Any]] = [
        {
            "Description": OPENING_MARKER,
            "Balance": _num(account.get("opening_balance")),
            "Currency": currency,
        }
    ]
    for txn in account.get("transactions") or []:
        math_ok = txn.get("math_ok")
        rows.append(
            {
                "Date": _fmt_date(txn.get("date")),
                "Description": txn.get("description") or "",
                "Withdrawal": _num(txn.get("withdrawal")),
                "Deposit": _num(txn.get("deposit")),
                "Balance": _num(txn.get("balance")),
                "Currency": currency,
                "Math_Check": "" if math_ok is None else ("OK" if math_ok else "FAIL"),
            }
        )
    rows.append({"Description": TOTALS_MARKER, "Currency": currency})
    return rows


def build_bank_workbook(
    statement: dict[str, Any],
    *,
    extract_mode: str | None = None,
) -> dict[str, Any]:
    """Project ReadBankStatement JSON into one workbook dict (one sheet per account)."""
    accounts = normalize_bank_statement(statement, extract_mode=extract_mode)
    sheets = []
    for account in accounts:
        sheets.append(
            {
                "title": account.get("sheet_title") or "Bank",
                "columns": list(BANK_COLS),
                "rows": _account_rows(account),
                "reconciled": bool(account.get("reconciled")),
                "reconcile_note": account.get("reconcile_note") or "",
                "extract_mode": account.get("extract_mode"),
                "bank_name": account.get("bank_name"),
                "account_number": account.get("account_number"),
                "currency": account.get("currency"),
            }
        )
    return {"sheet_count": len(sheets), "sheets": sheets}
