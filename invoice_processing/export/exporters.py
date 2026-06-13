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
from openpyxl.utils import get_column_letter

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


class LedgerExporter:
    """Base: write a Ledger_FY workbook (Sys_Config + Purchase + Sales)."""

    system: str = ""           # key into sg_gst.yaml code_map
    software_name: str = ""    # Sys_Config SOFTWARE value
    purchase_cols: list[str] = []
    sales_cols: list[str] = []

    def __init__(self, classifier: Optional[TaxClassifier] = None):
        self.clf = classifier or TaxClassifier()

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


_SHEET_INVALID = str.maketrans({c: None for c in "[]:*?/\\"})


def _sheet_title(name: str) -> str:
    return (name or "Bank").translate(_SHEET_INVALID).strip()[:31] or "Bank"


def _parse_ddmmyyyy(value) -> Optional[datetime]:
    """Parse a ``DD/MM/YYYY`` date string into a ``datetime`` (None if unparseable).

    Robust to ``date``/``datetime`` inputs and a few separator variants; never
    raises — an unparseable value yields ``None`` so callers can keep stable order
    instead of crashing on a malformed row.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except (ValueError, TypeError):
            continue
    return None


class BankStatementExporter:
    """Write an accountant-grade, continuous bank-statement workbook.

    One sheet per account; every txn row preserved. Unlike a per-statement-block
    layout, each account sheet is ONE continuous chain across the whole FY:

    - Statements (months) are sorted ascending by date regardless of upload order,
      and transactions within a month are sorted ascending by date too.
    - The running ``Balance`` (col E) chains top-to-bottom across the ENTIRE sheet:
      every txn row = ``=<prev_balance_cell> + Deposit - Withdrawal``, crossing
      month boundaries (it does NOT re-seed each month).
    - Each month keeps one ``BALANCE B/F`` marker row as a visual separator. Its
      running ``Balance`` *carries forward* from the prior row (``=<prev_E>``),
      while ``Stated Balance`` (col F) holds that month's stated opening B/F. The
      FIRST B/F of the FY seeds from its own stated opening (``=F<row>``).
    - A cross-month continuity ``Check`` (col G) on each B/F row flags ``GAP`` when
      the carried-forward balance ≠ the stated B/F (a missing/duplicate month);
      txn rows keep the per-row ``CHECK`` against their own stated balance.
    - A single per-account ``TOTALS`` row at the bottom sums all withdrawals/deposits.

    The sheet is REBUILT from a normalized list of month-blocks on every append, so
    the result is identical no matter what order statements arrived in.
    """

    BANK_COLS = [
        "Date", "Description", "Withdrawal", "Deposit",
        "Balance", "Currency", "Math_Check",
    ]

    #: Description markers used to detect row roles.
    OPENING_MARKER = "BALANCE B/F"
    TOTALS_MARKER = "TOTALS"

    def bank_rows(self, stmt: BankStatement) -> list[dict]:
        """Build the data rows for one statement (values only; formulas added later).

        The opening row seeds ``Stated Balance`` from the opening balance; each txn
        row carries its withdrawal/deposit + the extracted ``Stated Balance``. A
        trailing ``TOTALS`` row closes the block. These value rows are the unit a
        caller hands to the store as a batch; the store normalizes + re-sorts them
        into the continuous chain (formula cells are filled on rebuild).
        """
        rows: list[dict] = []
        rows.append({
            "Description": self.OPENING_MARKER,
            "Balance": _num(stmt.opening_balance),
            "Currency": stmt.currency,
        })
        for txn in stmt.transactions:
            rows.append({
                "Date": _fmt_date(txn.date),
                "Description": txn.description,
                "Withdrawal": _num(txn.withdrawal),
                "Deposit": _num(txn.deposit),
                "Balance": _num(txn.balance),
                "Currency": stmt.currency,
            })
        rows.append({
            "Description": self.TOTALS_MARKER,
            "Currency": stmt.currency,
        })
        return rows

    # ------------------------------------------------------------------ #
    # Continuous-chain rebuild
    # ------------------------------------------------------------------ #

    @classmethod
    def rows_to_blocks(cls, rows: list[dict]) -> list[dict]:
        """Split a flat list of value-row dicts (a statement) into month-blocks.

        Each ``BALANCE B/F`` marker starts a new block carrying its stated opening
        balance; subsequent non-marker rows are that block's transactions. ``TOTALS``
        markers are dropped (the per-account total is regenerated on rebuild). A
        leading run of transactions with no preceding B/F forms a headless block
        (``stated_bf=None``) so nothing is lost.
        """
        blocks: list[dict] = []
        current: Optional[dict] = None
        for row in rows:
            desc = row.get("Description")
            if desc == cls.OPENING_MARKER:
                current = {
                    "stated_bf": row.get("Balance"),
                    "currency": row.get("Currency"),
                    "transactions": [],
                }
                blocks.append(current)
            elif desc == cls.TOTALS_MARKER:
                continue
            else:
                if current is None:
                    current = {"stated_bf": None, "currency": row.get("Currency"), "transactions": []}
                    blocks.append(current)
                current["transactions"].append(dict(row))
        return blocks

    @classmethod
    def _block_sort_key(cls, block: dict):
        """Sort key for a month-block: earliest parseable txn date.

        Blocks whose dates are all unparseable sort last but keep stable relative
        order (Python's sort is stable), so a malformed month never crashes the build.
        """
        dates = [
            d for d in (_parse_ddmmyyyy(t.get("Date")) for t in block["transactions"])
            if d is not None
        ]
        return (0, min(dates)) if dates else (1, datetime.max)

    @classmethod
    def sort_blocks(cls, blocks: list[dict]) -> list[dict]:
        """Return month-blocks sorted ascending by date, txns sorted within each.

        Months are ordered by their earliest transaction date (upload-order
        independent); within a month, transactions are ordered ascending by date.
        Unparseable dates keep their stable relative position (never crash).
        """
        ordered = sorted(blocks, key=cls._block_sort_key)
        for block in ordered:
            block["transactions"].sort(
                key=lambda t: (0, _parse_ddmmyyyy(t.get("Date")))
                if _parse_ddmmyyyy(t.get("Date")) is not None
                else (1, datetime.max)
            )
        return ordered

    @classmethod
    def rebuild_account_sheet(
        cls,
        sheet,
        blocks: list[dict],
        cols: list[str],
        *,
        key_col: Optional[str] = None,
    ) -> None:
        """Wipe a sheet's body and rewrite it as one continuous chain of month-blocks.

        ``blocks`` is the normalized, already-sorted list from :meth:`sort_blocks`
        (each ``{"stated_bf", "currency", "transactions": [row_dict]}``). For each
        block we emit a ``BALANCE B/F`` row then its txn rows; a single ``TOTALS``
        row closes the whole account. The header row (row 1) is preserved; an
        optional ``key_col`` (the hidden dedupe column) is carried per-row from each
        row dict's ``key_col`` value so dedupe survives the rebuild.
        """
        # Clear everything below the header.
        if sheet.max_row > 1:
            sheet.delete_rows(2, sheet.max_row - 1)

        currency = ""
        for block in blocks:
            if block.get("currency"):
                currency = block["currency"]
                break

        def emit(row: dict) -> None:
            values = [row.get(c, "") for c in cols]
            if key_col is not None:
                values.append(row.get(key_col, ""))
            sheet.append(values)

        for block in blocks:
            emit({
                "Description": cls.OPENING_MARKER,
                "Balance": block.get("stated_bf"),
                "Currency": block.get("currency") or currency,
                key_col: block.get("bf_key", "") if key_col else "",
            })
            for txn in block["transactions"]:
                emit(txn)

        # Single per-account TOTALS row at the bottom.
        emit({"Description": cls.TOTALS_MARKER, "Currency": currency})

        cls.apply_bank_formulas(sheet, cols)

    @classmethod
    def apply_bank_formulas(cls, sheet, cols: Optional[list[str]] = None) -> None:
        """Write Math_Check formulas and TOTALS SUM over the sheet.

        Balance holds the actual bank-stated value on every row — no formula.
        Math_Check (col G) validates each row arithmetically:

        - ``BALANCE B/F`` row: first B/F of the FY gets ``✅`` (no prior row);
          later months get ``=IF(ROUND(E_bf-E_prev,2)=0,"✅","GAP")`` — a ``GAP``
          means the stated opening doesn't match the prior month's closing balance.
        - txn row: ``=IF(ROUND(E-(E_prev+Deposit-Withdrawal),2)=0,"✅","❌")``.
        - ``TOTALS`` row: ``=SUM(...)`` over the Withdrawal/Deposit txn range.
        """
        header = [c.value for c in sheet[1]] if sheet.max_row >= 1 else []
        if not header:
            return
        idx = {name: i for i, name in enumerate(header)}
        if "Balance" not in idx or "Description" not in idx:
            return

        def col(name: str) -> Optional[str]:
            i = idx.get(name)
            return get_column_letter(i + 1) if i is not None else None

        c_desc = idx["Description"]
        bal_col = col("Balance")
        check_col = col("Math_Check")
        wd_col = col("Withdrawal")
        dep_col = col("Deposit")

        prev_balance_row: Optional[int] = None
        seen_first_bf = False
        first_txn_row: Optional[int] = None
        last_txn_row: Optional[int] = None
        totals_row: Optional[int] = None

        for r in range(2, sheet.max_row + 1):
            desc = sheet.cell(row=r, column=c_desc + 1).value
            if desc == cls.OPENING_MARKER:
                if check_col and bal_col:
                    if not seen_first_bf or prev_balance_row is None:
                        # First B/F of the FY — no prior row to compare against.
                        sheet[f"{check_col}{r}"] = "✅"
                    else:
                        sheet[f"{check_col}{r}"] = (
                            f'=IF(ROUND({bal_col}{r}-{bal_col}{prev_balance_row},2)=0,"✅","GAP")'
                        )
                prev_balance_row = r
                seen_first_bf = True
            elif desc == cls.TOTALS_MARKER:
                totals_row = r
            else:
                if first_txn_row is None:
                    first_txn_row = r
                last_txn_row = r
                if check_col and bal_col and prev_balance_row is not None:
                    arithmetic = f"{bal_col}{prev_balance_row}"
                    if dep_col:
                        arithmetic += f"+{dep_col}{r}"
                    if wd_col:
                        arithmetic += f"-{wd_col}{r}"
                    sheet[f"{check_col}{r}"] = (
                        f'=IF(ROUND({bal_col}{r}-({arithmetic}),2)=0,"✅","❌")'
                    )
                prev_balance_row = r

        # TOTALS row: SUM over all txn rows for Withdrawal and Deposit.
        if totals_row is not None and first_txn_row is not None and last_txn_row is not None:
            if wd_col:
                sheet[f"{wd_col}{totals_row}"] = f"=SUM({wd_col}{first_txn_row}:{wd_col}{last_txn_row})"
            if dep_col:
                sheet[f"{dep_col}{totals_row}"] = f"=SUM({dep_col}{first_txn_row}:{dep_col}{last_txn_row})"

    def write_bank_workbook(self, path: str | Path, statements: list[BankStatement]) -> Path:
        """Write a workbook with one continuous-chain sheet per account.

        Statements sharing a bank/account sheet are merged into a single continuous
        ledger (sorted by date, one chain), so a year of monthly statements for an
        account lands as one sorted, cross-month-reconciling sheet.
        """
        wb = Workbook()
        # Group statements by their sheet title, preserving first-seen order.
        grouped: dict[str, list[BankStatement]] = {}
        for stmt in statements:
            grouped.setdefault(_sheet_title(stmt.bank_name), []).append(stmt)

        for i, (title, stmts) in enumerate(grouped.items()):
            sheet = wb.active if i == 0 else wb.create_sheet()
            sheet.title = title
            sheet.append(self.BANK_COLS)
            blocks: list[dict] = []
            for stmt in stmts:
                blocks.extend(self.rows_to_blocks(self.bank_rows(stmt)))
            self.rebuild_account_sheet(sheet, self.sort_blocks(blocks), self.BANK_COLS)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(path)
        return path


def get_bank_exporter() -> BankStatementExporter:
    return BankStatementExporter()
