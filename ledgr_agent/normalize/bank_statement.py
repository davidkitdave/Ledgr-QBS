"""Deterministic bank-statement normalization for the light path."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

_SHEET_INVALID = str.maketrans({c: None for c in "[]:*?/\\"})


def _sheet_title(name: str) -> str:
    return (name or "Bank").translate(_SHEET_INVALID).strip()[:31] or "Bank"


def _last4_digits(*sources: str | None) -> str:
    digits = "".join(c for c in "".join(s or "" for s in sources) if c.isdigit())
    return digits[-4:].rjust(4, "0") if digits else "0000"


def _bank_label(bank_name: str) -> str:
    raw = (bank_name or "").strip()
    if " - " in raw:
        return raw.split(" - ", 1)[0].strip()
    if "-" in raw and not any(c.isdigit() for c in raw.split("-", 1)[0]):
        return raw.split("-", 1)[0].strip()
    return raw or "Bank"


def bank_sheet_title(
    *,
    bank_name: str,
    account_number: str | None,
    currency: str,
) -> str:
    """Build ``"<Bank> - XXXX - CCY"`` for a bank statement Excel tab."""
    label = _bank_label(bank_name)
    last4 = _last4_digits(account_number, bank_name)
    ccy = (currency or "?").strip().upper() or "?"
    return _sheet_title(f"{label} - {last4} - {ccy}")


def _fmt_date(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (date, datetime)):
        return value.strftime("%d/%m/%Y")
    text = str(value).strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return text


def _num(value: Any) -> Any:
    if value is None or value == "":
        return ""
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return value


def reconcile_running_balance(
    account: dict[str, Any],
    *,
    tol_abs: float = 0.01,
    tol_rel: float = 0.001,
) -> tuple[bool, str]:
    """Verify prev - withdrawal + deposit == balance for each txn row."""
    prev = account.get("opening_balance")
    txns = account.get("transactions") or []
    failures = 0
    for txn in txns:
        balance = txn.get("balance")
        if balance is not None and prev is not None:
            withdrawal = txn.get("withdrawal") or 0.0
            deposit = txn.get("deposit") or 0.0
            expected = float(prev) - float(withdrawal) + float(deposit)
            tol = max(tol_abs, tol_rel * abs(expected))
            math_ok = abs(float(balance) - expected) <= tol
            txn["math_ok"] = math_ok
            if not math_ok:
                failures += 1
        else:
            txn["math_ok"] = None
        if balance is not None:
            prev = balance

    n = len(txns)
    reconciled = failures == 0
    note = (
        f"all {n} rows reconciled"
        if reconciled
        else f"{failures}/{n} rows fail running-balance check"
    )
    account["reconciled"] = reconciled
    account["reconcile_note"] = note
    return reconciled, note


def normalize_bank_statement(
    payload: dict[str, Any],
    *,
    extract_mode: str | None = None,
) -> list[dict[str, Any]]:
    """Map ReadBankStatement JSON to reconciled per-account dicts with sheet titles."""
    accounts_out: list[dict[str, Any]] = []
    for acct in payload.get("accounts") or []:
        if not isinstance(acct, dict):
            continue
        currency = (acct.get("currency") or "SGD").strip().upper() or "SGD"
        normalized = {
            "bank_name": acct.get("bank_name") or "",
            "account_number": acct.get("account_number"),
            "currency": currency,
            "statement_period": acct.get("statement_period"),
            "opening_balance": acct.get("opening_balance"),
            "closing_balance": acct.get("closing_balance"),
            "extract_mode": extract_mode,
            "transactions": [
                dict(t) if isinstance(t, dict) else t.model_dump()
                for t in (acct.get("transactions") or [])
            ],
        }
        normalized["sheet_title"] = bank_sheet_title(
            bank_name=normalized["bank_name"],
            account_number=normalized.get("account_number"),
            currency=currency,
        )
        reconcile_running_balance(normalized)
        accounts_out.append(normalized)
    return accounts_out
