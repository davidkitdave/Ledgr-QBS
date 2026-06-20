"""Deterministic ERP code resolution — tax, creditor, GL.

Client-provided master data always wins over YAML seeds. Blank when unknown — never guess.
"""

from __future__ import annotations

from typing import Any, Optional

from .categorizer import _norm
from .client_context import EntityMemoryEntry
from .models import InvoiceLine, NormalizedInvoice
from .tax_classifier import TaxClassifier


def _normalize_tax_codes(
    client_tax_codes: dict[str, str] | list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not client_tax_codes:
        return []
    if isinstance(client_tax_codes, dict):
        return [{"code": code, "description": desc} for code, desc in client_tax_codes.items()]
    return [dict(entry) for entry in client_tax_codes]


def _client_code_set(entries: list[dict[str, Any]]) -> set[str]:
    return {str(e.get("code") or "").strip() for e in entries if e.get("code")}


def _entry_matches_treatment(
    entry: dict[str, Any],
    treatment: str,
    rate: Optional[float],
) -> bool:
    key = str(entry.get("treatment") or "").strip()
    if not key:
        return False
    if ":" in key:
        t_part, r_part = key.split(":", 1)
        if t_part.strip().upper() != (treatment or "").strip().upper():
            return False
        try:
            expected = float(r_part)
        except ValueError:
            return False
        if rate is None:
            return False
        return abs(rate - expected) < 0.001
    return key.strip().upper() == (treatment or "").strip().upper()


def resolve_rate_for_line(
    classifier: TaxClassifier,
    line: InvoiceLine,
    inv: NormalizedInvoice,
) -> Optional[float]:
    """Pick the rate used for rate-keyed ERP tax codes."""
    treatment = line.tax_treatment
    if not treatment:
        return None
    allowed = classifier.allowed_rates_for_treatment(treatment, line, inv)
    if allowed:
        match = classifier._best_rate_match(line, allowed)
        if match:
            return match[0]
    if treatment == "IM":
        return classifier.imported_rate_for_date(inv.invoice_date)
    if treatment in ("SR", "SSR"):
        if treatment == "SSR" and allowed:
            return allowed[0]
        return classifier.standard_rate_for_date(inv.invoice_date)
    return None


def resolve_tax_code(
    treatment: Optional[str],
    *,
    rate: Optional[float],
    doc_type: str,
    software: str,
    client_tax_codes: dict[str, str] | list[dict[str, Any]] | None,
    classifier: TaxClassifier,
) -> str:
    """Resolve a target ERP tax code for ``treatment`` at ``rate``.

    Order:
      1. Client master entry keyed by ``treatment`` / ``treatment:rate``.
      2. YAML seed via ``classifier.tax_code``.
      3. When a client list exists, only return codes present in that list.
    """
    if not treatment:
        return ""

    entries = _normalize_tax_codes(client_tax_codes)
    for entry in entries:
        if _entry_matches_treatment(entry, treatment, rate):
            code = str(entry.get("code") or "").strip()
            if code:
                return code

    yaml_code = classifier.tax_code(treatment, doc_type, software, rate=rate) or ""
    if entries:
        allowed = _client_code_set(entries)
        if yaml_code and yaml_code in allowed:
            return yaml_code
        return ""
    return yaml_code


def resolve_creditor_code(
    vendor_name: Optional[str],
    reg_no: Optional[str],
    entity_memory: list[EntityMemoryEntry],
) -> str:
    """Deterministic creditor/vendor code from entity memory (name or reg-no match)."""
    n_vendor = _norm(vendor_name)
    n_reg = _norm(reg_no)

    for entry in entity_memory:
        code = (entry.creditor_code or "").strip()
        if not code:
            continue
        n_name = _norm(entry.name)
        name_hit = bool(n_name) and len(n_name) > 3 and n_vendor and (
            n_name in n_vendor or n_vendor in n_name
        )
        reg_hit = bool(n_reg) and bool(_norm(entry.reg_no)) and _norm(entry.reg_no) == n_reg
        if name_hit or reg_hit:
            return code
    return ""
