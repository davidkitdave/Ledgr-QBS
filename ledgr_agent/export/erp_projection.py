"""Self-contained YAML-driven ERP row projection for ledgr_agent.

Reads ``ledgr_agent/skills/{qbs,xero,autocount,sql_account}.yaml`` and maps a
plain document dict (from ``document_reader_agent``) into per-line ERP import
rows. No imports from ``invoice_processing`` or ``accounting_agents``.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

_SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"

_SKILL_FILES: dict[str, str] = {
    "qbs": "qbs.yaml",
    "xero": "xero.yaml",
    "autocount": "autocount.yaml",
    "sql_account": "sql_account.yaml",
}

DEFAULT_SYSTEMS: list[str] = ["qbs", "xero", "autocount", "sql_account"]

_SKILL_REQUIRED_KEYS = ("software_name", "system", "purchase_cols", "sales_cols")

_SKILL_CACHE: dict[str, dict[str, Any]] = {}


class ExportSkillError(RuntimeError):
    """Raised when an ERP skill YAML is missing or malformed."""


def normalize_system_key(system: str) -> str:
    """Return canonical exporter key for *system*."""
    key = (system or "").strip().lower().replace(" ", "_")
    aliases = {
        "qbs_ledger": "qbs",
        "sqlaccount": "sql_account",
    }
    return aliases.get(key, key)


def load_export_skill(system: str) -> dict[str, Any]:
    """Load (cached) the export skill for canonical *system*."""
    key = normalize_system_key(system)
    cached = _SKILL_CACHE.get(key)
    if cached is not None:
        return cached

    filename = _SKILL_FILES.get(key)
    if filename is None:
        raise ExportSkillError(
            f"no export skill registered for system '{system}'; have {sorted(_SKILL_FILES)}"
        )
    path = _SKILLS_DIR / filename
    if not path.is_file():
        raise ExportSkillError(f"export skill file missing: {path}")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ExportSkillError(
            f"export skill '{path}' must be a YAML mapping, got {type(data).__name__}"
        )
    for req in _SKILL_REQUIRED_KEYS:
        if req not in data:
            raise ExportSkillError(f"export skill '{path}' is missing required key '{req}'")
    if data["system"] != key:
        raise ExportSkillError(
            f"export skill '{path}' declares system '{data['system']}' but is registered as '{key}'"
        )

    _SKILL_CACHE[key] = data
    return data


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


def _doc_sign(document_kind: str) -> int:
    return -1 if (document_kind or "").strip().lower() == "credit_note" else 1


def _signed_amount(value: Any, sign: int) -> float:
    if value is None or value == "":
        return 0.0
    return round(float(value) * sign, 2)


def _line_context(document: dict[str, Any], line: dict[str, Any], doc_type: str) -> dict[str, Any]:
    sign = _doc_sign(str(document.get("document_kind") or ""))
    qty_raw = line.get("quantity")
    qty = 1.0 if qty_raw is None or qty_raw == "" else float(qty_raw)

    # Light fallback for partial extraction: if the model left net_amount
    # blank but printed total_amount on the line, derive net as
    # total_amount minus the inferred tax. If a single-line bill left
    # tax_amount blank but the header tax_total is populated, fall back
    # to the header tax. Keeps QBS Sub Total / Tax non-zero when the LLM
    # skimmed the printed amounts.
    tax_raw = line.get("tax_amount")
    if tax_raw is None or tax_raw == "":
        header_tax = document.get("tax_total")
        lines_list = document.get("lines") or []
        if (
            header_tax is not None
            and header_tax != ""
            and len(lines_list) == 1
        ):
            tax_raw = header_tax
    tax = _signed_amount(tax_raw, sign)

    net_raw = line.get("net_amount")
    if net_raw is None or net_raw == "":
        total_raw = line.get("total_amount")
        if total_raw is not None and total_raw != "":
            net_raw = float(total_raw) - (float(tax_raw) if tax_raw not in (None, "") else 0.0)
    net = _signed_amount(net_raw, sign)
    total = round(net + tax, 2)

    vendor = (document.get("vendor_name") or "").strip()
    customer = (document.get("customer_name") or "").strip()
    party_name = vendor if doc_type == "purchase" else customer

    fx = document.get("fx_rate")
    fx_display = fx if fx is not None and fx != "" else ""

    invoice_date = _fmt_date(document.get("invoice_date"))
    due_date = _fmt_date(document.get("due_date") or document.get("invoice_date"))

    unit_amount_raw = line.get("unit_amount")
    if unit_amount_raw is not None and unit_amount_raw != "":
        unit_amount = _signed_amount(unit_amount_raw, sign)
    else:
        unit_amount = round(net / qty, 2) if qty else net

    grand_total = document.get("grand_total")
    invoice_total = (
        _signed_amount(grand_total, sign) if grand_total is not None and grand_total != "" else total
    )

    return {
        "invoice_number": document.get("invoice_number") or "",
        "invoice_date": invoice_date,
        "due_date": due_date,
        "vendor_name": vendor,
        "customer_name": customer,
        "contact_name": party_name,
        "entity_tax_id": document.get("entity_tax_id") or "",
        "description": line.get("description") or "",
        "sub_total": net,
        "taxable_amount": net,
        "tax_amount": tax,
        "total_amount": total,
        "total": invoice_total,
        "source_amount": net,
        "account_code": "",
        "tax_code": "",
        "creditor_code": "",
        "debtor_code": "",
        "currency": document.get("currency") or "",
        "currency_rate": fx_display,
        "supplier_invoice_no": document.get("invoice_number") or "",
        "unit_price": round(net / qty, 2) if qty else net,
        "qty": qty,
        "quantity": qty,
        "unit_amount": unit_amount,
        "uom": "UNIT",
    }


def _is_profile_skill(skill: dict[str, Any]) -> bool:
    return skill.get("exporter") == "profile" or bool(skill.get("purchase_fields"))


def _sheet_title(skill: dict[str, Any], doc_type: str) -> str:
    if doc_type == "sales":
        return str(skill.get("sales_sheet") or "Sales")
    return str(skill.get("purchase_sheet") or "Purchase")


def _resolve_doc_type(document: dict[str, Any]) -> str:
    doc_type = (document.get("doc_type") or "purchase").strip().lower()
    return doc_type if doc_type in ("purchase", "sales") else "purchase"


def _project_system(document: dict[str, Any], system: str) -> dict[str, Any]:
    skill = load_export_skill(system)
    doc_type = _resolve_doc_type(document)
    lines = [ln for ln in (document.get("lines") or []) if isinstance(ln, dict)]

    if _is_profile_skill(skill):
        cols_key = "sales_cols" if doc_type == "sales" else "purchase_cols"
        fields_key = "sales_fields" if doc_type == "sales" else "purchase_fields"
        constants_key = "sales_constants" if doc_type == "sales" else "purchase_constants"
        cols = list(skill[cols_key])
        field_map = dict(skill.get(fields_key) or {})
        constants = dict(skill.get(constants_key) or {})
        rows: list[dict[str, Any]] = []
        for line in lines:
            ctx = _line_context(document, line, doc_type)
            row = {col: ctx.get(field_map.get(col, ""), "") for col in cols}
            for col, val in constants.items():
                if col in row:
                    row[col] = val
            rows.append(row)
        sheet = _sheet_title(skill, doc_type)
    else:
        cols_key = "sales_cols" if doc_type == "sales" else "purchase_cols"
        cols = list(skill[cols_key])
        logical = dict(skill.get("logical_fields") or {})
        rows = []
        for line in lines:
            ctx = _line_context(document, line, doc_type)
            row: dict[str, Any] = {}
            for col in cols:
                key = logical.get(col, "")
                val = ctx.get(key, "") if key else ""
                row[col] = _num(val) if key in {
                    "sub_total", "tax_amount", "total", "total_amount",
                    "source_amount", "unit_amount", "quantity", "qty",
                    "taxable_amount", "unit_price",
                } and val != "" else val
            rows.append(row)
        sheet = "Sales" if doc_type == "sales" else "Purchase"

    return {
        "software_name": skill.get("software_name") or system,
        "sheet": sheet,
        "columns": cols,
        "rows": rows,
    }


def project(document: dict[str, Any], systems: list[str] | None = None) -> dict[str, Any]:
    """Project *document* into ERP import rows for each requested *systems* key."""
    targets = [normalize_system_key(s) for s in (systems or DEFAULT_SYSTEMS)]
    results: dict[str, Any] = {}
    for system in targets:
        results[system] = _project_system(document, system)
    return {"systems": targets, "results": results}
