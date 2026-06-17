"""Shared helpers for assistant tool modules."""

from __future__ import annotations


def filename_matches_query(needle: str, stored: str) -> bool:
    """Return True when ``needle`` identifies ``stored`` (full or partial).

    Users often say ``25-D15`` while the processing log has
    ``25-D15-Company-A.pdf`` and Xero ledger rows group as
    ``Xero:25-D15``. Match all three shapes.
    """
    n = (needle or "").strip().lower()
    s = (stored or "").strip().lower()
    if not n or not s:
        return False
    if n == s or n in s or s in n:
        return True
    n_bare = n[5:] if n.startswith("xero:") else n
    s_bare = s[5:] if s.startswith("xero:") else s
    if n_bare and (n_bare == s_bare or n_bare in s_bare or s_bare in n_bare):
        return True
    return False


def row_search_text(row: dict) -> str:
    """Concatenate ledger columns that ``lookup_row`` should search."""
    cols = (
        "Description",
        "description",
        "Vendor",
        "vendor",
        "Reference",
        "Source Filename",
        "source_filename",
        "*InvoiceNumber",
        "*ContactName",
        "*Description",
        "Account Code / COA",
        "account_code",
        "category",
    )
    return " ".join(str(row.get(col) or "") for col in cols).lower()


def _normalize_coa_code(code: str) -> str:
    return (code or "").strip().lower().replace(" ", "")


def find_coa_by_code(state: dict, account_code: str) -> dict | None:
    """Return the COA dict for ``account_code`` (exact then normalized match)."""
    needle = (account_code or "").strip()
    if not needle:
        return None
    coa_list = state.get("coa") or []
    if not isinstance(coa_list, list):
        return None
    needle_norm = _normalize_coa_code(needle)
    exact: dict | None = None
    fuzzy: dict | None = None
    for entry in coa_list:
        if not isinstance(entry, dict):
            continue
        ec = str(entry.get("code") or "").strip()
        if not ec:
            continue
        if ec == needle or ec.lower() == needle.lower():
            return entry
        if _normalize_coa_code(ec) == needle_norm:
            fuzzy = entry
    return fuzzy
