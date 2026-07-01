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


def _parse_date(value: Any) -> date | None:
    text = _fmt_date(value)
    if not text:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _sort_transactions_by_date(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return transactions sorted ascending by date; undated rows keep stable tail order."""

    def sort_key(txn: dict[str, Any]) -> tuple:
        parsed = _parse_date(txn.get("date"))
        return (0, parsed) if parsed is not None else (1, date.max)

    return sorted(transactions, key=sort_key)


def recompute_transaction_balances(
    *,
    opening_balance: float | None,
    transactions: list[dict[str, Any]],
) -> None:
    """Rewrite each txn ``balance`` from opening + running wd/dep arithmetic."""
    running: float | None = (
        float(opening_balance) if opening_balance is not None else None
    )
    for txn in transactions:
        withdrawal = txn.get("withdrawal") or 0.0
        deposit = txn.get("deposit") or 0.0
        if running is not None:
            running = round(
                running - float(withdrawal or 0) + float(deposit or 0),
                2,
            )
            txn["balance"] = running
        elif txn.get("balance") is not None:
            running = float(txn["balance"])


def _maybe_reverse_printed_order(account: dict[str, Any]) -> None:
    """When Gemini returns newest-first rows, flip to chronological before sort."""
    txns = account.get("transactions") or []
    if len(txns) < 2:
        return

    def _trial(rows: list[dict[str, Any]]) -> bool:
        trial = {
            "opening_balance": account.get("opening_balance"),
            "transactions": [dict(t) for t in rows],
        }
        ok, _ = reconcile_running_balance(trial)
        return ok

    if _trial(txns):
        return
    reversed_txns = list(reversed(txns))
    if _trial(reversed_txns):
        account["transactions"] = reversed_txns


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
        _maybe_reverse_printed_order(normalized)
        normalized["transactions"] = _sort_transactions_by_date(normalized["transactions"])
        recompute_transaction_balances(
            opening_balance=normalized.get("opening_balance"),
            transactions=normalized["transactions"],
        )
        reconcile_running_balance(normalized)
        accounts_out.append(normalized)
    return accounts_out
