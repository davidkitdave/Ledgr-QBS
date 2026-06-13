"""Hermetic tests for ``SlackLedgerStore`` — fetch → append → re-upload.

Mocks Slack (an in-memory file store keyed by id, with ``files_upload_v2`` /
``files_info`` + an opener that serves the stored bytes) and Firestore (the
shared ``tests._fake_firestore.FakeFirestore``). No network, no real Slack.

Proves:
- A first append starts a fresh workbook and writes rows into the right sheet.
- A second append fetches the current workbook (via the pointer) and adds the new
  batch — both batches end up in the workbook.
- Re-processing the SAME document (same dedupe doc_key) does NOT double-append.
- The Firestore pointer is updated to the latest uploaded file id.
"""

from __future__ import annotations

import io
import uuid

from openpyxl import load_workbook

from accounting_agents.ledger_store import SlackLedgerStore
from tests._fake_firestore import FakeFirestore


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    """Serves bytes for a previously-recorded https URL → bytes mapping."""

    def __init__(self, urls: dict[str, bytes]):
        self._urls = urls

    def open(self, req):
        url = req.full_url if hasattr(req, "full_url") else req
        return _FakeResponse(self._urls[url])


class FakeSlackClient:
    """In-memory Slack file store supporting upload + info + download + reactions."""

    token = "xoxb-test"

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.urls: dict[str, bytes] = {}
        self.uploads: list[dict] = []
        self._posts: list[dict] = []
        self.updates: list[dict] = []
        # Reaction tracking: list of {"channel", "timestamp", "name"} dicts.
        self.reactions_added: list[dict] = []
        self.reactions_removed: list[dict] = []
        # Optional per-file share ts injected by tests to simulate files_info shares.
        # Maps file_id → {"channel_id": ts_string}.
        self._file_share_ts: dict[str, dict[str, str]] = {}
        # Tracks deleted file ids (Fix 1: old ledger file cleanup).
        self.deleted_file_ids: list[str] = []

    def chat_postMessage(self, **kwargs):
        self._posts.append(kwargs)
        return {"ok": True, "ts": f"{len(self._posts)}.000"}

    def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ok": True}

    def files_upload_v2(self, *, channel, filename, file, title=None):
        file_id = "F" + uuid.uuid4().hex[:10]
        self.files[file_id] = file
        url = f"https://files.slack.com/{file_id}/{filename}"
        self.urls[url] = file
        self.uploads.append({"channel": channel, "filename": filename, "id": file_id})
        return {"files": [{"id": file_id, "url_private_download": url}]}

    def files_info(self, *, file):
        # Build shares from _file_share_ts if present, so tests can exercise the
        # reaction path. Also include url_private_download for ledger-store tests.
        shares: dict = {}
        if file in self._file_share_ts:
            shares["private"] = {
                ch: [{"ts": ts}]
                for ch, ts in self._file_share_ts[file].items()
            }
        if file in self.files:
            data = self.files[file]
            url = next(u for u, b in self.urls.items() if b is data)
        else:
            url = f"https://files.slack.com/{file}/unknown"
        return {"file": {"id": file, "url_private_download": url, "name": "ledger.xlsx", "shares": shares}}

    def files_delete(self, *, file):
        self.deleted_file_ids.append(file)
        self.files.pop(file, None)
        return {"ok": True}

    def reactions_add(self, *, channel, timestamp, name):
        self.reactions_added.append({"channel": channel, "timestamp": timestamp, "name": name})
        return {"ok": True}

    def reactions_remove(self, *, channel, timestamp, name):
        self.reactions_removed.append({"channel": channel, "timestamp": timestamp, "name": name})
        return {"ok": True}

    def opener(self) -> _FakeOpener:
        return _FakeOpener(self.urls)


def _row(desc: str) -> dict:
    return {
        "Invoice Number": "INV-1",
        "Description": desc,
        "Source Amount": 100.0,
        "Account Code / COA": "6000",
    }


def _read_sheet_rows(data: bytes, sheet: str) -> list[tuple]:
    wb = load_workbook(io.BytesIO(data))
    ws = wb[sheet]
    return list(ws.iter_rows(min_row=2, values_only=True))


def _make_store(slack: FakeSlackClient) -> SlackLedgerStore:
    return SlackLedgerStore(FakeFirestore(), opener=slack.opener())


def test_first_append_starts_fresh_workbook():
    slack = FakeSlackClient()
    store = _make_store(slack)

    result = store.append_rows(
        client_id="c1",
        fy="2026",
        slack_client=slack,
        channel_id="C1",
        software="qbs",
        kind="invoice",
        batches=[{"sheet": "Purchase", "doc_key": "F1:Purchase:INV-1", "rows": [_row("first")]}],
    )

    assert result["appended"] == 1
    assert result["deduped"] == 0
    assert result["slack_file_id"]
    data = slack.files[result["slack_file_id"]]
    rows = _read_sheet_rows(data, "Purchase")
    assert len(rows) == 1
    # Pointer updated.
    ptr = store.get_pointer("c1", "2026")
    assert ptr["slack_file_id"] == result["slack_file_id"]


def test_second_append_fetches_and_adds_batch():
    slack = FakeSlackClient()
    store = _make_store(slack)

    store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1",
        software="qbs", kind="invoice",
        batches=[{"sheet": "Purchase", "doc_key": "F1:Purchase:INV-1", "rows": [_row("first")]}],
    )
    result2 = store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1",
        software="qbs", kind="invoice",
        batches=[{"sheet": "Purchase", "doc_key": "F2:Purchase:INV-2", "rows": [_row("second")]}],
    )

    assert result2["appended"] == 1
    data = slack.files[result2["slack_file_id"]]
    rows = _read_sheet_rows(data, "Purchase")
    # Both batches present in the workbook now.
    descriptions = [r for row in rows for r in row if r in ("first", "second")]
    assert "first" in descriptions and "second" in descriptions
    assert len(rows) == 2
    # Pointer advanced to the newest upload.
    assert store.get_pointer("c1", "2026")["slack_file_id"] == result2["slack_file_id"]


def test_reprocessing_same_doc_is_deduped():
    slack = FakeSlackClient()
    store = _make_store(slack)

    store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1",
        software="qbs", kind="invoice",
        batches=[{"sheet": "Purchase", "doc_key": "F1:Purchase:INV-1", "rows": [_row("first")]}],
    )
    # Re-process the SAME document (same doc_key) → no double-append.
    result2 = store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1",
        software="qbs", kind="invoice",
        batches=[{"sheet": "Purchase", "doc_key": "F1:Purchase:INV-1", "rows": [_row("first")]}],
    )

    assert result2["appended"] == 0
    assert result2["deduped"] == 1
    data = slack.files[result2["slack_file_id"]]
    rows = _read_sheet_rows(data, "Purchase")
    assert len(rows) == 1  # still exactly one row


def test_bank_workbook_one_sheet_per_account():
    slack = FakeSlackClient()
    store = _make_store(slack)

    result = store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1",
        kind="bank",
        batches=[
            {"sheet": "OCBC - 5001", "doc_key": "F1:OCBC:1", "rows": [{"Date": "01/02/2026", "Description": "x", "Balance": 10.0}]},
            {"sheet": "DBS - 9002", "doc_key": "F1:DBS:1", "rows": [{"Date": "01/02/2026", "Description": "y", "Balance": 20.0}]},
        ],
    )

    data = slack.files[result["slack_file_id"]]
    wb = load_workbook(io.BytesIO(data))
    assert "OCBC - 5001" in wb.sheetnames
    assert "DBS - 9002" in wb.sheetnames
    # No hidden dedupe column in the workbook — dedupe is Firestore-side.
    header = [c.value for c in wb["OCBC - 5001"][1]]
    assert "_ledgr_doc_key" not in header


def test_bank_sheet_has_no_dedupe_column():
    """The written bank sheet must contain NO _ledgr_doc_key column at all."""
    slack = FakeSlackClient()
    store = _make_store(slack)
    from invoice_processing.export.exporters import BankStatementExporter
    from invoice_processing.export.models import BankStatement, BankTransaction
    from datetime import date as _date

    exporter = BankStatementExporter()
    stmt = BankStatement(
        bank_name="OCBC - 5001",
        currency="SGD",
        opening_balance=1000.0,
        closing_balance=900.0,
        transactions=[
            BankTransaction(date=_date(2025, 4, 1), description="ATM", withdrawal=100.0, deposit=None, balance=900.0)
        ],
    )
    result = store.append_rows(
        client_id="c1", fy="2025", slack_client=slack, channel_id="C1",
        kind="bank",
        batches=[{"sheet": stmt.bank_name, "doc_key": "F1:OCBC:1", "rows": exporter.bank_rows(stmt)}],
    )
    wb = load_workbook(io.BytesIO(slack.files[result["slack_file_id"]]))
    header = [c.value for c in wb["OCBC - 5001"][1]]
    assert "_ledgr_doc_key" not in header, f"Unexpected dedupe column in header: {header}"


def test_dedupe_via_firestore_no_duplicate_rows():
    """Re-appending the same doc_key must not add duplicate rows — dedupe is Firestore-side."""
    slack = FakeSlackClient()
    db = FakeFirestore()
    store = SlackLedgerStore(db, opener=slack.opener())

    store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1",
        software="qbs", kind="invoice",
        batches=[{"sheet": "Purchase", "doc_key": "F1:Purchase:INV-1", "rows": [_row("first")]}],
    )
    # Verify the seen_doc_keys were persisted to Firestore.
    ptr = store.get_pointer("c1", "2026")
    assert "F1:Purchase:INV-1" in ptr.get("seen_doc_keys", [])

    # Re-append the SAME doc_key — must be deduped via Firestore (no sheet read needed).
    result2 = store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1",
        software="qbs", kind="invoice",
        batches=[{"sheet": "Purchase", "doc_key": "F1:Purchase:INV-1", "rows": [_row("first")]}],
    )
    assert result2["appended"] == 0
    assert result2["deduped"] == 1
    data = slack.files[result2["slack_file_id"]]
    rows = _read_sheet_rows(data, "Purchase")
    assert len(rows) == 1  # still exactly one row


def test_second_append_deletes_first_file():
    """After a second append, the FIRST (superseded) Slack file must be deleted."""
    slack = FakeSlackClient()
    store = _make_store(slack)

    result1 = store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1",
        software="qbs", kind="invoice",
        batches=[{"sheet": "Purchase", "doc_key": "F1:Purchase:INV-1", "rows": [_row("first")]}],
    )
    first_file_id = result1["slack_file_id"]

    result2 = store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1",
        software="qbs", kind="invoice",
        batches=[{"sheet": "Purchase", "doc_key": "F2:Purchase:INV-2", "rows": [_row("second")]}],
    )
    second_file_id = result2["slack_file_id"]

    # The first file was deleted after the second upload succeeded.
    assert first_file_id in slack.deleted_file_ids
    # The second file (latest) was NOT deleted.
    assert second_file_id not in slack.deleted_file_ids
    # The pointer points at the second file.
    assert store.get_pointer("c1", "2026")["slack_file_id"] == second_file_id


def test_first_append_does_not_delete_any_file():
    """On the very first append there is no previous file — nothing should be deleted."""
    slack = FakeSlackClient()
    store = _make_store(slack)

    store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1",
        software="qbs", kind="invoice",
        batches=[{"sheet": "Purchase", "doc_key": "F1:Purchase:INV-1", "rows": [_row("first")]}],
    )
    assert slack.deleted_file_ids == []


# --------------------------------------------------------------------------- #
# Accountant-grade bank export: live formulas, no legacy columns
# --------------------------------------------------------------------------- #

from datetime import date  # noqa: E402

from invoice_processing.export.exporters import BankStatementExporter  # noqa: E402
from invoice_processing.export.models import BankStatement, BankTransaction  # noqa: E402


def _bank_stmt(
    name: str,
    opening: float,
    txns: list[tuple],
    closing: float,
    txn_date: date = date(2026, 2, 1),
) -> BankStatement:
    """Build a BankStatement; each txn = (description, withdrawal, deposit, stated_balance).

    All transactions share ``txn_date`` (override per statement to exercise the
    cross-month, date-sorted continuous chain).
    """
    return BankStatement(
        bank_name=name,
        currency="SGD",
        opening_balance=opening,
        closing_balance=closing,
        transactions=[
            BankTransaction(
                date=txn_date,
                description=d,
                withdrawal=w,
                deposit=dep,
                balance=bal,
            )
            for (d, w, dep, bal) in txns
        ],
    )


def _bank_batch(exporter, stmt, doc_key):
    return {"sheet": stmt.bank_name, "doc_key": doc_key, "rows": exporter.bank_rows(stmt)}


def test_bank_export_no_legacy_columns():
    exporter = BankStatementExporter()
    header = exporter.BANK_COLS
    assert "Notes" not in header
    assert "Source File ID" not in header
    assert "Stated Balance" not in header
    assert "Check" not in header
    # Rosebery-pattern columns present.
    assert "Balance" in header
    assert "Math_Check" in header
    assert "Currency" in header


def test_bank_balance_and_check_are_live_formulas():
    slack = FakeSlackClient()
    store = _make_store(slack)
    exporter = BankStatementExporter()

    stmt = _bank_stmt(
        "OCBC - 5001", 1000.0,
        [("ATM withdrawal", 100.0, None, 900.0), ("Salary", None, 500.0, 1400.0)],
        1400.0,
    )
    result = store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1", kind="bank",
        batches=[_bank_batch(exporter, stmt, "F1:OCBC:1")],
    )

    wb = load_workbook(io.BytesIO(slack.files[result["slack_file_id"]]), data_only=False)
    ws = wb["OCBC - 5001"]
    header = [c.value for c in ws[1]]
    bi = header.index("Balance") + 1
    ci = header.index("Math_Check") + 1
    wi = header.index("Withdrawal") + 1
    di = header.index("Deposit") + 1

    # Row layout: 1=header, 2=BALANCE B/F, 3=txn, 4=txn, 5=TOTALS.
    # Balance is always an actual number (never a formula).
    assert not str(ws.cell(row=2, column=bi).value).startswith("=")  # B/F row
    assert not str(ws.cell(row=3, column=bi).value).startswith("=")  # txn row
    assert not str(ws.cell(row=4, column=bi).value).startswith("=")  # txn row
    # First B/F Math_Check is a static ✅ (no prior row to compare).
    assert ws.cell(row=2, column=ci).value == "✅"
    # Txn Math_Check is an IF/ROUND formula referencing balance and arithmetic.
    chk = ws.cell(row=3, column=ci).value
    assert isinstance(chk, str) and chk.startswith("=IF(ROUND(")
    # Totals row has SUM formulas over the txn block.
    totals_wd = ws.cell(row=5, column=wi).value
    totals_dep = ws.cell(row=5, column=di).value
    assert str(totals_wd).startswith("=SUM(") and str(totals_dep).startswith("=SUM(")


def test_bank_formulas_correct_after_second_append():
    slack = FakeSlackClient()
    store = _make_store(slack)
    exporter = BankStatementExporter()

    stmt1 = _bank_stmt("OCBC - 5001", 1000.0, [("w1", 100.0, None, 900.0)], 900.0)
    store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1", kind="bank",
        batches=[_bank_batch(exporter, stmt1, "F1:OCBC:1")],
    )
    stmt2 = _bank_stmt("OCBC - 5001", 900.0, [("d1", None, 250.0, 1150.0)], 1150.0)
    result2 = store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1", kind="bank",
        batches=[_bank_batch(exporter, stmt2, "F2:OCBC:2")],
    )

    wb = load_workbook(io.BytesIO(slack.files[result2["slack_file_id"]]), data_only=False)
    ws = wb["OCBC - 5001"]
    header = [c.value for c in ws[1]]
    bi = header.index("Balance") + 1
    di = header.index("Description") + 1
    ci = header.index("Math_Check") + 1

    from openpyxl.utils import get_column_letter
    bal = get_column_letter(bi)

    # Continuous layout after two appends (months merged into one chain, single TOTALS):
    # 1 header
    # 2 B/F (stmt1)  3 w1
    # 4 B/F (stmt2)  5 d1
    # 6 TOTALS
    descs = [ws.cell(row=r, column=di).value for r in range(2, ws.max_row + 1)]
    assert descs == ["BALANCE B/F", "w1", "BALANCE B/F", "d1", "TOTALS"]

    # Balance is always an actual number from the bank statement, never a formula.
    for r in (2, 3, 4, 5):
        assert not str(ws.cell(row=r, column=bi).value).startswith("="), f"row {r} Balance should be a number"

    # First B/F Math_Check: static ✅ (no prior row).
    assert ws.cell(row=2, column=ci).value == "✅"
    # Second B/F Math_Check: continuity formula — carried balance (E3) vs this B/F (E4).
    assert ws.cell(row=4, column=ci).value == f'=IF(ROUND({bal}4-{bal}3,2)=0,"✅","GAP")'
    # Txn Math_Check: arithmetic formula referencing prior balance row.
    assert ws.cell(row=5, column=ci).value.startswith("=IF(ROUND(")
    # No hidden dedupe column — the Excel is clean / human-readable.
    assert "_ledgr_doc_key" not in header


# --------------------------------------------------------------------------- #
# Edge-case coverage: descending balance, overdraft, wrong balance, continuity
# --------------------------------------------------------------------------- #

def _build_sheet(rows_dicts, cols=None):
    """Build a populated worksheet from value-row dicts; return (ws, header_idx)."""
    from openpyxl import Workbook as _WB
    exp = BankStatementExporter()
    cols = cols or exp.BANK_COLS
    wb = _WB()
    ws = wb.active
    ws.append(list(cols))
    for row in rows_dicts:
        ws.append([row.get(c, "") for c in cols])
    exp.apply_bank_formulas(ws, cols)
    idx = {c.value: c.column for c in ws[1]}
    return ws, idx


def test_descending_balance_balance_is_always_number():
    """All Balance cells are actual numbers even when balance falls every row."""
    stmt = BankStatement(
        bank_name="DBS - 9999", currency="SGD", opening_balance=5000.0, closing_balance=2300.0,
        transactions=[
            BankTransaction(date=date(2025, 6, 1),  description="Rent",      withdrawal=1500.0, deposit=None,  balance=3500.0),
            BankTransaction(date=date(2025, 6, 5),  description="Utilities", withdrawal=200.0,  deposit=None,  balance=3300.0),
            BankTransaction(date=date(2025, 6, 10), description="Insurance", withdrawal=1000.0, deposit=None,  balance=2300.0),
        ],
    )
    exp = BankStatementExporter()
    ws, idx = _build_sheet(exp.bank_rows(stmt))
    bi = idx["Balance"]
    # Rows 2 (B/F), 3,4,5 (txns) — all numbers, never formulas.
    for r in range(2, 6):
        v = ws.cell(row=r, column=bi).value
        assert not str(v).startswith("="), f"row {r} Balance should be a number, got {v!r}"
    assert ws.cell(row=2, column=bi).value == 5000.0
    assert ws.cell(row=3, column=bi).value == 3500.0
    assert ws.cell(row=4, column=bi).value == 3300.0
    assert ws.cell(row=5, column=bi).value == 2300.0


def test_descending_balance_math_check_formula_chains():
    """Math_Check formulas reference the correct prior-row Balance cells."""
    stmt = BankStatement(
        bank_name="DBS - 9999", currency="SGD", opening_balance=5000.0, closing_balance=2300.0,
        transactions=[
            BankTransaction(date=date(2025, 6, 1), description="Rent",      withdrawal=1500.0, deposit=None, balance=3500.0),
            BankTransaction(date=date(2025, 6, 5), description="Utilities", withdrawal=200.0,  deposit=None, balance=3300.0),
            BankTransaction(date=date(2025, 6, 10),description="Insurance", withdrawal=1000.0, deposit=None, balance=2300.0),
        ],
    )
    exp = BankStatementExporter()
    ws, idx = _build_sheet(exp.bank_rows(stmt))
    from openpyxl.utils import get_column_letter
    bal = get_column_letter(idx["Balance"])
    dep = get_column_letter(idx["Deposit"])
    wd  = get_column_letter(idx["Withdrawal"])
    ci  = idx["Math_Check"]

    # B/F row (row 2): static ✅, no prior row.
    assert ws.cell(row=2, column=ci).value == "✅"
    # Txn rows: formula references prior Balance and this row's Deposit/Withdrawal.
    assert ws.cell(row=3, column=ci).value == f'=IF(ROUND({bal}3-({bal}2+{dep}3-{wd}3),2)=0,"✅","❌")'
    assert ws.cell(row=4, column=ci).value == f'=IF(ROUND({bal}4-({bal}3+{dep}4-{wd}4),2)=0,"✅","❌")'
    assert ws.cell(row=5, column=ci).value == f'=IF(ROUND({bal}5-({bal}4+{dep}5-{wd}5),2)=0,"✅","❌")'
    # TOTALS row (row 6): no Math_Check.
    assert ws.cell(row=6, column=ci).value in (None, "")


def test_overdraft_negative_balance():
    """Negative (overdraft) balances are stored as-is and formulas still chain correctly."""
    stmt = BankStatement(
        bank_name="OD - 0001", currency="SGD", opening_balance=100.0, closing_balance=-450.0,
        transactions=[
            BankTransaction(date=date(2025, 6, 1), description="Big payment", withdrawal=550.0, deposit=None,   balance=-450.0),
            BankTransaction(date=date(2025, 6, 5), description="Refund",       withdrawal=None,  deposit=100.0, balance=-350.0),
        ],
    )
    exp = BankStatementExporter()
    ws, idx = _build_sheet(exp.bank_rows(stmt))
    bi = idx["Balance"]
    assert ws.cell(row=2, column=bi).value == 100.0    # B/F opening
    assert ws.cell(row=3, column=bi).value == -450.0   # after big withdrawal
    assert ws.cell(row=4, column=bi).value == -350.0   # after refund
    # Math_Check formulas still present and well-formed.
    ci = idx["Math_Check"]
    assert ws.cell(row=3, column=ci).value.startswith("=IF(ROUND(")
    assert ws.cell(row=4, column=ci).value.startswith("=IF(ROUND(")


def test_wrong_balance_math_check_formula_structure():
    """When the bank states a wrong balance, the Math_Check formula exposes it.

    We can't evaluate Excel formulas in openpyxl, but we verify the formula
    compares E_curr (the stated value) against the arithmetic — so Excel will
    show ❌ when they differ.
    """
    # Txn says balance=500 but arithmetic (1000-600=400) gives 400 — wrong.
    stmt = BankStatement(
        bank_name="ERR - 0001", currency="SGD", opening_balance=1000.0, closing_balance=500.0,
        transactions=[
            BankTransaction(date=date(2025, 6, 1), description="Wrong bal txn", withdrawal=600.0, deposit=None, balance=500.0),
        ],
    )
    exp = BankStatementExporter()
    ws, idx = _build_sheet(exp.bank_rows(stmt))
    from openpyxl.utils import get_column_letter
    bal = get_column_letter(idx["Balance"])
    dep = get_column_letter(idx["Deposit"])
    wd  = get_column_letter(idx["Withdrawal"])
    ci  = idx["Math_Check"]

    # Balance holds the bank's stated value (500 — wrong, but faithfully stored).
    assert ws.cell(row=3, column=idx["Balance"]).value == 500.0
    # Math_Check formula references E3 (stated=500) vs arithmetic E2+D3-C3 (=400).
    # In Excel this resolves to ❌ because ROUND(500-400,2)≠0.
    expected = f'=IF(ROUND({bal}3-({bal}2+{dep}3-{wd}3),2)=0,"✅","❌")'
    assert ws.cell(row=3, column=ci).value == expected


def test_cross_month_descending_chain_balance_and_continuity():
    """Two descending-balance months: second B/F continuity formula references prior closing."""
    exp = BankStatementExporter()
    slack = FakeSlackClient()
    store = _make_store(slack)

    stmt1 = _bank_stmt("DBS - 8888", 5000.0,
                       [("Rent", 2000.0, None, 3000.0), ("Bills", 500.0, None, 2500.0)],
                       2500.0, txn_date=date(2025, 6, 15))
    stmt2 = _bank_stmt("DBS - 8888", 2500.0,
                       [("Salary", None, 3000.0, 5500.0), ("Tax", 1000.0, None, 4500.0)],
                       4500.0, txn_date=date(2025, 7, 15))

    store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1", kind="bank",
        batches=[_bank_batch(exp, stmt1, "F1:DBS:1")],
    )
    result = store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1", kind="bank",
        batches=[_bank_batch(exp, stmt2, "F2:DBS:2")],
    )

    wb = load_workbook(io.BytesIO(slack.files[result["slack_file_id"]]), data_only=False)
    ws = wb["DBS - 8888"]
    header = [c.value for c in ws[1]]
    bi = header.index("Balance") + 1
    ci = header.index("Math_Check") + 1
    di = header.index("Description") + 1

    from openpyxl.utils import get_column_letter
    bal = get_column_letter(bi)

    # Layout: 1=hdr, 2=B/F(jun), 3=Rent, 4=Bills, 5=B/F(jul), 6=Salary, 7=Tax, 8=TOTALS
    descs = [ws.cell(row=r, column=di).value for r in range(2, ws.max_row + 1)]
    assert descs == ["BALANCE B/F", "Rent", "Bills", "BALANCE B/F", "Salary", "Tax", "TOTALS"]

    # Balance is a real number on every data row.
    for r in range(2, 8):
        v = ws.cell(row=r, column=bi).value
        assert not str(v).startswith("="), f"row {r} Balance should be a number"
    assert ws.cell(row=2, column=bi).value == 5000.0
    assert ws.cell(row=3, column=bi).value == 3000.0
    assert ws.cell(row=4, column=bi).value == 2500.0
    assert ws.cell(row=5, column=bi).value == 2500.0   # second B/F stated opening
    assert ws.cell(row=6, column=bi).value == 5500.0
    assert ws.cell(row=7, column=bi).value == 4500.0

    # First B/F Math_Check: ✅ (no prior month).
    assert ws.cell(row=2, column=ci).value == "✅"
    # Second B/F continuity check: compares E5 (stated 2500) against E4 (closing 2500).
    assert ws.cell(row=5, column=ci).value == f'=IF(ROUND({bal}5-{bal}4,2)=0,"✅","GAP")'
    # Txn Math_Check formulas all well-formed.
    for r in (3, 4, 6, 7):
        assert ws.cell(row=r, column=ci).value.startswith("=IF(ROUND("), f"row {r} missing formula"


def test_cross_month_gap_is_flagged_by_formula():
    """When month 2 B/F opening ≠ month 1 closing, the GAP formula exposes it.

    We verify the formula references the right cells so Excel would evaluate to GAP.
    """
    exp = BankStatementExporter()
    slack = FakeSlackClient()
    store = _make_store(slack)

    stmt1 = _bank_stmt("OCBC - 7777", 1000.0, [("w1", 100.0, None, 900.0)], 900.0,
                       txn_date=date(2025, 6, 15))
    # Second month's stated opening is 1000 — but month 1 closed at 900 (GAP of 100).
    stmt2 = _bank_stmt("OCBC - 7777", 1000.0, [("d1", None, 50.0, 1050.0)], 1050.0,
                       txn_date=date(2025, 7, 15))

    store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1", kind="bank",
        batches=[_bank_batch(exp, stmt1, "F1:OCBC:1")],
    )
    result = store.append_rows(
        client_id="c1", fy="2026", slack_client=slack, channel_id="C1", kind="bank",
        batches=[_bank_batch(exp, stmt2, "F2:OCBC:2")],
    )

    wb = load_workbook(io.BytesIO(slack.files[result["slack_file_id"]]), data_only=False)
    ws = wb["OCBC - 7777"]
    header = [c.value for c in ws[1]]
    bi = header.index("Balance") + 1
    ci = header.index("Math_Check") + 1

    from openpyxl.utils import get_column_letter
    bal = get_column_letter(bi)

    # Layout: 1=hdr, 2=B/F(jun), 3=w1, 4=B/F(jul), 5=d1, 6=TOTALS
    # Second B/F (row 4): stated=1000, prior closing (row 3)=900 → formula will show GAP.
    assert ws.cell(row=4, column=bi).value == 1000.0   # stated opening stored faithfully
    continuity = ws.cell(row=4, column=ci).value
    assert continuity == f'=IF(ROUND({bal}4-{bal}3,2)=0,"✅","GAP")'
    # (In Excel: ROUND(1000-900,2)=100≠0 → "GAP")
