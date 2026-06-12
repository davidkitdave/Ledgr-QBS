"""Accounting-ledger exporters — emit the client's `<Client> - Ledger_FY<year>.xlsx`
workbook (sheets: Sys_Config, Purchase, Sales), matching the native formats observed in
the client data.

Two target formats, chosen by the client's `ACCOUNTING_SOFTWARE` setting:
- **QBS Ledger** (`QbsLedgerExporter`): native QBS columns; no tax-code column (SR/ZR is
  implied by the Tax Amount being 9% vs 0).
- **Xero Ledger** (`XeroLedgerExporter`): Xero import columns + `Source File ID` / `[AI Status]`,
  carrying the explicit `*TaxType` string.

Each invoice line becomes one row, so a mixed SR/ZR telco bill yields two rows. The tax
treatment + amount come from the shared `TaxClassifier`; account codes come from the COA
categorization layer (later phase). Adding a target = one subclass with its column lists +
row mapper.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Optional

from openpyxl import Workbook

from .models import BankStatement, InvoiceLine, NormalizedInvoice
from .tax_classifier import TaxClassifier, classify_invoice


def _fmt_date(d) -> str:
    if d is None:
        return ""
    if isinstance(d, (date, datetime)):
        return d.strftime("%d/%m/%Y")
    return str(d)


def _num(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(float(x), 2)


def _tax_amount(line: InvoiceLine, inv: NormalizedInvoice, clf: TaxClassifier) -> float:
    if line.tax_treatment != "SR":
        return 0.0
    if line.gst_amount:
        return round(float(line.gst_amount), 2)
    if line.net_amount:
        return round(float(line.net_amount) * clf.rate_for_date(inv.invoice_date), 2)
    return 0.0


def _invoice_total(inv: NormalizedInvoice, clf: TaxClassifier) -> float:
    """Invoice-level grand total. Prefer the authoritative doc total carried from
    extraction; otherwise fall back to Σ line net + Σ line tax."""
    if inv.doc_total is not None:
        return round(float(inv.doc_total), 2)
    net = sum((line.net_amount or 0.0) for line in inv.lines)
    tax = sum(_tax_amount(line, inv, clf) for line in inv.lines)
    return round(net + tax, 2)


class LedgerExporter:
    """Base: write a Ledger_FY workbook (Sys_Config + Purchase + Sales)."""

    system: str = ""           # key into sg_gst.yaml code_map
    software_name: str = ""    # Sys_Config SOFTWARE value
    purchase_cols: list[str] = []
    sales_cols: list[str] = []

    def __init__(self, classifier: Optional[TaxClassifier] = None):
        self.clf = classifier or TaxClassifier()

    def required_fields(self, doc_type: str) -> list[str]:
        """Column names that must be non-empty in every exported row for this
        software. Subclasses override; used by the pipeline to flag (not drop)
        documents that would export half-filled rows."""
        return []

    # subclasses implement the per-line row dicts
    def _purchase_row(self, inv: NormalizedInvoice, line: InvoiceLine) -> dict:
        raise NotImplementedError

    def _sales_row(self, inv: NormalizedInvoice, line: InvoiceLine) -> dict:
        raise NotImplementedError

    def _ensure_classified(self, invoices: list[NormalizedInvoice]) -> None:
        for inv in invoices:
            if any(l.tax_treatment is None for l in inv.lines):
                classify_invoice(inv, self.clf)

    def rows(self, invoices: list[NormalizedInvoice], doc_type: str) -> list[dict]:
        self._ensure_classified(invoices)
        builder = self._sales_row if doc_type == "sales" else self._purchase_row
        out = []
        for inv in invoices:
            for line in inv.lines:
                out.append(builder(inv, line))
        return out

    def write_workbook(
        self,
        path: str | Path,
        purchases: Optional[list[NormalizedInvoice]] = None,
        sales: Optional[list[NormalizedInvoice]] = None,
    ) -> Path:
        """Write a Ledger workbook with just Purchase + Sales sheets (no Sys_Config)."""
        purchases, sales = purchases or [], sales or []
        wb = Workbook()
        for i, (title, cols, invs, doc) in enumerate([
            ("Purchase", self.purchase_cols, purchases, "purchase"),
            ("Sales", self.sales_cols, sales, "sales"),
        ]):
            sheet = wb.active if i == 0 else wb.create_sheet(title)
            sheet.title = title
            sheet.append(cols)
            for row in self.rows(invs, doc):
                sheet.append([row.get(c, "") for c in cols])
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(path)
        return path


class QbsLedgerExporter(LedgerExporter):
    """Native QBS Ledger format (no tax-code column; Tax Amount carries SR 9% / ZR 0)."""

    system = "qbs"
    software_name = "QBS Ledger"
    purchase_cols = [
        "Invoice Number", "Invoice Date", "Vendor Name", "Entity Tax ID", "Description",
        "Source Amount", "Currency", "Currency Rate", "Sub Total", "Tax Amount",
        "Total Amount", "Account Code / COA",
    ]
    sales_cols = [
        "Invoice Date", "Invoice Number", "Customer Name", "Description", "Source Amount",
        "Currency", "Currency Rate", "Amount", "Tax Amount", "Total", "Account Code / COA",
    ]

    def required_fields(self, doc_type: str) -> list[str]:
        if doc_type == "sales":
            return [
                "Invoice Number", "Invoice Date", "Customer Name", "Amount", "Total",
                "Account Code / COA",
            ]
        return [
            "Invoice Number", "Invoice Date", "Vendor Name", "Sub Total", "Total Amount",
            "Account Code / COA",
        ]

    def _purchase_row(self, inv, line):
        net = _num(line.net_amount) or 0
        tax = _tax_amount(line, inv, self.clf)
        return {
            "Invoice Number": inv.invoice_number or "",
            "Invoice Date": _fmt_date(inv.invoice_date),
            "Vendor Name": inv.supplier.name or "",
            "Entity Tax ID": inv.supplier.gst_regno or "",
            "Description": line.description,
            "Source Amount": net,
            "Currency": inv.currency,
            "Currency Rate": 1.0,
            "Sub Total": net,
            "Tax Amount": tax,
            "Total Amount": round(net + tax, 2),
            "Account Code / COA": line.account_code or "",
        }

    def _sales_row(self, inv, line):
        net = _num(line.net_amount) or 0
        tax = _tax_amount(line, inv, self.clf)
        return {
            "Invoice Date": _fmt_date(inv.invoice_date),
            "Invoice Number": inv.invoice_number or "",
            "Customer Name": inv.customer.name or "",
            "Description": line.description,
            "Source Amount": net,
            "Currency": inv.currency,
            "Currency Rate": 1.0,
            "Amount": net,
            "Tax Amount": tax,
            "Total": round(net + tax, 2),
            "Account Code / COA": line.account_code or "",
        }


class XeroLedgerExporter(LedgerExporter):
    """Xero Ledger format: Xero import columns + Source File ID / [AI Status], explicit *TaxType."""

    system = "xero"
    software_name = "Xero Ledger"
    _XERO_PURCHASE = [
        "*ContactName", "EmailAddress", "POAddressLine1", "POAddressLine2", "POAddressLine3",
        "POAddressLine4", "POCity", "PORegion", "POPostalCode", "POCountry", "*InvoiceNumber",
        "*InvoiceDate", "*DueDate", "Total", "InventoryItemCode", "Description", "*Quantity",
        "*UnitAmount", "*AccountCode", "*TaxType", "TaxAmount", "TrackingName1", "TrackingOption1",
        "TrackingName2", "TrackingOption2", "Currency",
    ]
    _XERO_SALES = [
        "*ContactName", "EmailAddress", "POAddressLine1", "POAddressLine2", "POAddressLine3",
        "POAddressLine4", "POCity", "PORegion", "POPostalCode", "POCountry", "*InvoiceNumber",
        "Reference", "*InvoiceDate", "*DueDate", "Total", "InventoryItemCode", "*Description",
        "*Quantity", "*UnitAmount", "Discount", "*AccountCode", "*TaxType", "TaxAmount",
        "TrackingName1", "TrackingOption1", "TrackingName2", "TrackingOption2", "Currency",
        "BrandingTheme",
    ]
    purchase_cols = list(_XERO_PURCHASE)
    sales_cols = list(_XERO_SALES)

    def required_fields(self, doc_type: str) -> list[str]:
        """Xero-required columns = the `*`-marked headers."""
        cols = self.sales_cols if doc_type == "sales" else self.purchase_cols
        return [c for c in cols if c.startswith("*")]

    def _xero_common(self, inv, line):
        party = inv.counterparty
        tax_type = self.clf.tax_code(line.tax_treatment, inv.doc_type, "xero")
        qty = line.quantity if line.quantity is not None else 1
        unit = line.unit_amount
        if unit is None and line.net_amount is not None:
            unit = line.net_amount / qty if qty else line.net_amount
        return {
            "*ContactName": party.name or "",
            "POCountry": party.country or "",
            "*InvoiceNumber": inv.invoice_number or "",
            "*InvoiceDate": _fmt_date(inv.invoice_date),
            "*DueDate": _fmt_date(inv.due_date or inv.invoice_date),
            "Total": _invoice_total(inv, self.clf),
            "*Quantity": _num(qty),
            "*UnitAmount": _num(unit),
            "*AccountCode": line.account_code or "",
            "*TaxType": tax_type,
            "TaxAmount": _tax_amount(line, inv, self.clf),
            "Currency": inv.currency,
        }

    def _purchase_row(self, inv, line):
        row = self._xero_common(inv, line)
        row["Description"] = line.description
        return row

    def _sales_row(self, inv, line):
        row = self._xero_common(inv, line)
        row["*Description"] = line.description
        return row


EXPORTERS = {"qbs": QbsLedgerExporter, "xero": XeroLedgerExporter}


def get_exporter(system: str, classifier: Optional[TaxClassifier] = None) -> LedgerExporter:
    key = (system or "").strip().lower()
    if "xero" in key:
        return XeroLedgerExporter(classifier)
    if "qbs" in key:
        return QbsLedgerExporter(classifier)
    raise ValueError(f"unknown export system '{system}'; have {list(EXPORTERS)}")


# Map exporter column headers to readable snake_case names for review notes.
_FIELD_LABELS = {
    "*ContactName": "contact_name", "*InvoiceNumber": "invoice_number",
    "*InvoiceDate": "invoice_date", "*DueDate": "due_date", "*Quantity": "quantity",
    "*UnitAmount": "unit_amount", "*AccountCode": "account_code", "*TaxType": "tax_type",
    "*Description": "description",
    "Vendor Name": "vendor_name", "Customer Name": "customer_name",
    "Invoice Number": "invoice_number", "Invoice Date": "invoice_date",
    "Sub Total": "sub_total", "Total Amount": "total", "Amount": "amount", "Total": "total",
    "Account Code / COA": "account_code",
}


def _field_label(col: str) -> str:
    return _FIELD_LABELS.get(col, col.lstrip("*"))


def _is_empty(value) -> bool:
    """A required cell is missing when it is None or a blank/whitespace string."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def validate_required_fields(
    inv: NormalizedInvoice, exporter: LedgerExporter, doc_type: str
) -> list[str]:
    """Return readable labels for export-required fields that are missing/empty.

    Builds the rows exactly as they would be exported and checks the software's
    required columns. Invoice-level fields (empty on every line) are reported once;
    line-level fields are reported with a 1-based line number. Empty list = complete.
    """
    required = exporter.required_fields(doc_type)
    if not required:
        return []
    rows = exporter.rows([inv], doc_type)
    if not rows:
        # No lines were extracted — flag the doc so it surfaces for review rather
        # than being silently written as an empty shell or silently dropped.
        return ["no line items extracted"]
    n = len(rows)
    missing: list[str] = []
    for col in required:
        # *DueDate is filled from invoice_date as an export fallback; validate the
        # source field so a genuinely missing due_date is still flagged for review.
        if col == "*DueDate" and inv.due_date is None:
            missing.append(_field_label(col))
            continue
        empty_lines = [i for i, row in enumerate(rows, start=1) if _is_empty(row.get(col, ""))]
        if not empty_lines:
            continue
        label = _field_label(col)
        if len(empty_lines) == n:
            # invoice-level (missing on every line) — report once, no line number
            missing.append(label)
        else:
            for i in empty_lines:
                missing.append(f"{label} (line {i})")
    return missing


_SHEET_INVALID = str.maketrans({c: None for c in "[]:*?/\\"})


def _sheet_title(name: str) -> str:
    return (name or "Bank").translate(_SHEET_INVALID).strip()[:31] or "Bank"


class BankStatementExporter:
    """Write a bank-statement workbook: one sheet per account, every txn row preserved."""

    BANK_COLS = [
        "Date", "Description", "Withdrawal", "Deposit", "Balance",
        "Currency", "Math_Check", "Notes", "Source File ID",
    ]

    def bank_rows(self, stmt: BankStatement) -> list[dict]:
        rows: list[dict] = []
        if stmt.opening_balance is not None:
            rows.append({
                "Description": "BALANCE B/F",
                "Balance": _num(stmt.opening_balance),
                "Currency": stmt.currency,
                "Math_Check": "✅",
                "Source File ID": stmt.source_file_id or "",
            })
        for txn in stmt.transactions:
            if txn.math_ok is True:
                check = "✅"
            elif txn.math_ok is False:
                check = "⚠️"
            else:
                check = ""
            rows.append({
                "Date": _fmt_date(txn.date),
                "Description": txn.description,
                "Withdrawal": _num(txn.withdrawal),
                "Deposit": _num(txn.deposit),
                "Balance": _num(txn.balance),
                "Currency": stmt.currency,
                "Math_Check": check,
                "Notes": txn.note or "",
                "Source File ID": stmt.source_file_id or "",
            })
        return rows

    def write_bank_workbook(self, path: str | Path, statements: list[BankStatement]) -> Path:
        wb = Workbook()
        used: dict[str, int] = {}
        for i, stmt in enumerate(statements):
            title = _sheet_title(stmt.bank_name)
            if title in used:
                used[title] += 1
                suffix = f" ({used[title]})"
                title = title[: 31 - len(suffix)] + suffix
            else:
                used[title] = 0
            sheet = wb.active if i == 0 else wb.create_sheet()
            sheet.title = title
            sheet.append(self.BANK_COLS)
            for row in self.bank_rows(stmt):
                sheet.append([row.get(c, "") for c in self.BANK_COLS])
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(path)
        return path


def get_bank_exporter() -> BankStatementExporter:
    return BankStatementExporter()
