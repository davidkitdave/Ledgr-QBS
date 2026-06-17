"""Validate a parsed COA before persisting (ADR-0006).

QBS-style exports often omit account codes; rows are keyed by description.
Codes are optional but must be unique and well-formed when present.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_INCOME_TYPES = frozenset({
    "revenue", "income", "sales", "other income", "otherincome",
})
_EXPENSE_TYPES = frozenset({
    "expense", "expenses", "cost of sales", "cos", "direct costs",
    "overhead", "overheads", "cost",
})


@dataclass
class CoaValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _code_style(code: str) -> str:
    if code.isdigit():
        return "numeric"
    if re.match(r"^[A-Za-z0-9]+$", code):
        return "alphanumeric"
    if re.match(r"^[A-Za-z0-9]+(-[A-Za-z0-9]+)+$", code):
        return "hyphenated"
    return "other"


def _is_income(row: dict) -> bool:
    at = _norm(row.get("account_type", ""))
    return at in _INCOME_TYPES or any(k in at for k in ("revenue", "income", "sales"))


def _is_expense(row: dict) -> bool:
    at = _norm(row.get("account_type", ""))
    return at in _EXPENSE_TYPES or any(k in at for k in ("expense", "cost", "overhead"))


def validate_coa(rows: list[dict]) -> CoaValidationResult:
    """Return validation outcome for parsed COA rows."""
    errors: list[str] = []
    warnings: list[str] = []

    if not rows:
        return CoaValidationResult(ok=False, errors=["No accounts found in the file."])

    seen_codes: dict[str, int] = {}
    seen_descriptions: dict[str, int] = {}
    has_income = False
    has_expense = False
    code_styles: set[str] = set()

    for i, row in enumerate(rows, start=1):
        desc = (row.get("description") or "").strip()
        atype = (row.get("account_type") or "").strip()
        code = (row.get("code") or "").strip()

        if not desc:
            errors.append(f"Row {i}: description is required.")
        if not atype:
            errors.append(f"Row {i}: account type is required.")

        if code:
            if len(code) > 10:
                errors.append(f"Row {i}: account code '{code}' exceeds 10 characters.")
            if code in seen_codes:
                errors.append(
                    f"Duplicate account code '{code}' (rows {seen_codes[code]} and {i})."
                )
            else:
                seen_codes[code] = i
            code_styles.add(_code_style(code))

        nd = _norm(desc)
        if nd:
            if nd in seen_descriptions:
                warnings.append(
                    f"Duplicate description '{desc}' (rows {seen_descriptions[nd]} and {i})."
                )
            else:
                seen_descriptions[nd] = i

        if _is_income(row):
            has_income = True
        if _is_expense(row):
            has_expense = True

    if len(code_styles) > 1:
        warnings.append(
            "Account codes use mixed formats — consider a consistent code scheme throughout."
        )
    if "other" in code_styles:
        errors.append(
            "Account codes must be numeric, alphanumeric, or hyphen-separated (max 10 chars)."
        )

    if not has_income:
        errors.append(
            "COA must include at least one income/revenue account."
        )
    if not has_expense:
        errors.append(
            "COA must include at least one expense account."
        )

    return CoaValidationResult(ok=not errors, errors=errors, warnings=warnings)
