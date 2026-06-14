"""Targeted tests for the Q&A agent tools and SlackLedgerStore.read_rows.

Covers:
- ``summarize_by_category``: biggest expense category, multi-category totals.
- ``pnl_for_fy``: revenue/expense split, net calculation.
- ``gst_threshold_check``: below / near / above threshold.
- Empty-ledger graceful case for all three tools.
- ``SlackLedgerStore.read_rows`` round-trip with a fake Slack workbook.

No live Slack, no live Gemini — all fakes / pure function calls.
"""

from __future__ import annotations

import io
import json

import pytest
from openpyxl import Workbook

from accounting_agents.ledger_store import SlackLedgerStore
from accounting_agents.qa_agent import (
    LEDGER_DATA_KEY,
    gst_threshold_check,
    pnl_for_fy,
    summarize_by_category,
)
from tests._fake_firestore import FakeFirestore


# --------------------------------------------------------------------------- #
# Minimal ToolContext stub
# --------------------------------------------------------------------------- #


class _FakeToolContext:
    """Minimal stand-in for google.adk.tools.ToolContext (state dict only)."""

    def __init__(self, state: dict):
        self.state = state


def _ctx(rows: list[dict]) -> _FakeToolContext:
    return _FakeToolContext({LEDGER_DATA_KEY: rows})


def _empty_ctx() -> _FakeToolContext:
    return _FakeToolContext({})


# --------------------------------------------------------------------------- #
# Sample rows
# --------------------------------------------------------------------------- #


def _purchase_rows() -> list[dict]:
    return [
        {"Account Code / COA": "6100-Software", "Source Amount": 500.0, "Doc Type": "P", "Tax Rate": "SR"},
        {"Account Code / COA": "6100-Software", "Source Amount": 300.0, "Doc Type": "P", "Tax Rate": "SR"},
        {"Account Code / COA": "6200-Rent",     "Source Amount": 2000.0, "Doc Type": "P", "Tax Rate": "ES"},
        {"Account Code / COA": "6300-Travel",   "Source Amount": 150.0, "Doc Type": "P", "Tax Rate": "ZR"},
    ]


def _sales_rows() -> list[dict]:
    return [
        {"Account Code / COA": "4000-Revenue", "Source Amount": 10000.0, "Doc Type": "S", "Tax Rate": "SR"},
        {"Account Code / COA": "4000-Revenue", "Source Amount": 5000.0,  "Doc Type": "S", "Tax Rate": "SR"},
    ]


def _mixed_rows() -> list[dict]:
    return _purchase_rows() + _sales_rows()


# --------------------------------------------------------------------------- #
# summarize_by_category
# --------------------------------------------------------------------------- #


class TestSummarizeByCategory:
    def test_empty_ledger_returns_not_loaded(self):
        result = summarize_by_category(_empty_ctx())
        assert "not loaded" in result.lower()

    def test_totals_per_category(self):
        result = summarize_by_category(_ctx(_purchase_rows()))
        data = json.loads(result)
        totals = data["totals"]
        assert totals["6100-Software"] == pytest.approx(800.0)
        assert totals["6200-Rent"] == pytest.approx(2000.0)
        assert totals["6300-Travel"] == pytest.approx(150.0)

    def test_biggest_category_is_first(self):
        result = summarize_by_category(_ctx(_purchase_rows()))
        data = json.loads(result)
        keys = list(data["totals"].keys())
        # 6200-Rent (2000) should be first (sorted descending).
        assert keys[0] == "6200-Rent"

    def test_sales_and_purchases_combined(self):
        result = summarize_by_category(_ctx(_mixed_rows()))
        data = json.loads(result)
        assert "4000-Revenue" in data["totals"]
        assert data["totals"]["4000-Revenue"] == pytest.approx(15000.0)


# --------------------------------------------------------------------------- #
# pnl_for_fy
# --------------------------------------------------------------------------- #


class TestPnlForFy:
    def test_empty_ledger_returns_not_loaded(self):
        result = pnl_for_fy(_empty_ctx())
        assert "not loaded" in result.lower()

    def test_revenue_expenses_net(self):
        result = pnl_for_fy(_ctx(_mixed_rows()))
        data = json.loads(result)
        assert data["revenue"] == pytest.approx(15000.0)
        assert data["expenses"] == pytest.approx(2950.0)  # 500+300+2000+150
        assert data["net"] == pytest.approx(15000.0 - 2950.0)

    def test_pure_expenses_gives_zero_revenue(self):
        result = pnl_for_fy(_ctx(_purchase_rows()))
        data = json.loads(result)
        assert data["revenue"] == pytest.approx(0.0)
        assert data["expenses"] == pytest.approx(2950.0)
        assert data["net"] == pytest.approx(-2950.0)

    def test_fallback_sign_based_when_no_doc_type(self):
        rows = [
            {"Source Amount": 1000.0},   # no Doc Type → positive = revenue
            {"Source Amount": -400.0},   # no Doc Type → negative = expense
        ]
        result = pnl_for_fy(_ctx(rows))
        data = json.loads(result)
        assert data["revenue"] == pytest.approx(1000.0)
        assert data["expenses"] == pytest.approx(400.0)
        assert data["net"] == pytest.approx(600.0)


# --------------------------------------------------------------------------- #
# gst_threshold_check
# --------------------------------------------------------------------------- #


class TestGstThresholdCheck:
    def test_empty_ledger_returns_not_loaded(self):
        result = gst_threshold_check(_empty_ctx())
        assert "not loaded" in result.lower()

    def test_below_threshold(self):
        rows = [{"Source Amount": 100_000.0, "Tax Rate": "SR", "Doc Type": "S"}]
        result = gst_threshold_check(_ctx(rows))
        data = json.loads(result)
        assert data["taxable_turnover"] == pytest.approx(100_000.0)
        assert not data["near_threshold"]
        assert not data["already_exceeded"]
        assert data["headroom"] == pytest.approx(900_000.0)

    def test_near_threshold_flag(self):
        # 850 000 >= 80 % of 1 000 000 → near_threshold = True
        rows = [{"Source Amount": 850_000.0, "Tax Rate": "SR", "Doc Type": "S"}]
        result = gst_threshold_check(_ctx(rows))
        data = json.loads(result)
        assert data["near_threshold"] is True
        assert not data["already_exceeded"]

    def test_above_threshold(self):
        rows = [{"Source Amount": 1_200_000.0, "Tax Rate": "SR", "Doc Type": "S"}]
        result = gst_threshold_check(_ctx(rows))
        data = json.loads(result)
        assert data["already_exceeded"] is True
        assert data["headroom"] == pytest.approx(0.0)

    def test_exempt_rows_excluded(self):
        rows = [
            {"Source Amount": 900_000.0, "Tax Rate": "SR"},   # counts
            {"Source Amount": 500_000.0, "Tax Rate": "ES"},   # exempt — excluded
        ]
        result = gst_threshold_check(_ctx(rows))
        data = json.loads(result)
        # Only SR row counts → 900 000, not 1 400 000.
        assert data["taxable_turnover"] == pytest.approx(900_000.0)

    def test_zero_rated_counts_toward_threshold(self):
        rows = [{"Source Amount": 800_000.0, "Tax Rate": "ZR"}]
        result = gst_threshold_check(_ctx(rows))
        data = json.loads(result)
        assert data["taxable_turnover"] == pytest.approx(800_000.0)


# --------------------------------------------------------------------------- #
# SlackLedgerStore.read_rows round-trip
# --------------------------------------------------------------------------- #


def _build_fake_xlsx(sheets: dict[str, list[dict]]) -> bytes:
    """Build an in-memory xlsx workbook with the given sheet→rows mapping.

    Each row dict key becomes a column header. The workbook contains no hidden
    dedupe column — dedupe state is now fully Firestore-side.
    """
    wb = Workbook()
    first = True
    for sheet_name, rows in sheets.items():
        if first:
            ws = wb.active
            ws.title = sheet_name
            first = False
        else:
            ws = wb.create_sheet(sheet_name)
        if not rows:
            continue
        headers = list(rows[0].keys())
        ws.append(headers)
        for row in rows:
            ws.append(list(row.values()))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


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
    def __init__(self, url_to_bytes: dict[str, bytes]):
        self._map = url_to_bytes

    def open(self, req):
        url = req.full_url if hasattr(req, "full_url") else req
        return _FakeResponse(self._map[url])


class _FakeSlackForRead:
    """Minimal Slack fake that serves one file (download only)."""

    token = "xoxb-test"

    def __init__(self, file_bytes: bytes, file_id: str = "FTEST001"):
        self._file_id = file_id
        self._file_bytes = file_bytes
        self._url = f"https://files.slack.com/{file_id}/ledger.xlsx"

    def files_info(self, *, file):
        assert file == self._file_id
        return {"file": {"id": file, "url_private_download": self._url}}

    def opener(self) -> _FakeOpener:
        return _FakeOpener({self._url: self._file_bytes})


def _make_store_with_pointer(
    slack: _FakeSlackForRead, client_id: str = "c1", fy: str = "2026"
) -> SlackLedgerStore:
    db = FakeFirestore()
    store = SlackLedgerStore(db, opener=slack.opener())
    # Manually seed the Firestore pointer so read_rows can find the file.
    store._pointer_ref(client_id, fy).set(
        {"slack_file_id": slack._file_id, "client_id": client_id, "fy": fy}
    )
    return store


class TestReadRows:
    def test_returns_empty_list_when_no_pointer(self):
        db = FakeFirestore()
        store = SlackLedgerStore(db)
        result = store.read_rows("c1", "2026", slack_client=None, channel_id="C1")
        assert result == []

    def test_reads_all_rows_from_purchase_sheet(self):
        purchase_rows = [
            {"Invoice Number": "INV-1", "Source Amount": 100.0, "Account Code / COA": "6000"},
            {"Invoice Number": "INV-2", "Source Amount": 200.0, "Account Code / COA": "6100"},
        ]
        xlsx = _build_fake_xlsx({"Purchase": purchase_rows})
        slack = _FakeSlackForRead(xlsx)
        store = _make_store_with_pointer(slack)

        rows = store.read_rows("c1", "2026", slack_client=slack, channel_id="C1")
        assert len(rows) == 2
        amounts = {r["Invoice Number"]: r["Source Amount"] for r in rows}
        assert amounts["INV-1"] == pytest.approx(100.0)
        assert amounts["INV-2"] == pytest.approx(200.0)

    def test_no_internal_keys_in_output(self):
        """read_rows must never expose internal / system columns to callers."""
        purchase_rows = [{"Invoice Number": "INV-1", "Source Amount": 50.0}]
        xlsx = _build_fake_xlsx({"Purchase": purchase_rows})
        slack = _FakeSlackForRead(xlsx)
        store = _make_store_with_pointer(slack)

        rows = store.read_rows("c1", "2026", slack_client=slack, channel_id="C1")
        # The workbook has no dedupe column; none should leak into the output.
        assert "_ledgr_doc_key" not in rows[0]
        # The data columns are present.
        assert rows[0]["Invoice Number"] == "INV-1"

    def test_sheet_name_injected_as_sheet_key(self):
        rows_data = [{"Amount": 99.0}]
        xlsx = _build_fake_xlsx({"Purchase": rows_data})
        slack = _FakeSlackForRead(xlsx)
        store = _make_store_with_pointer(slack)

        rows = store.read_rows("c1", "2026", slack_client=slack, channel_id="C1")
        assert rows[0]["_sheet"] == "Purchase"

    def test_reads_multiple_sheets(self):
        purchase = [{"Invoice Number": "P1", "Source Amount": 10.0}]
        sales = [{"Invoice Number": "S1", "Source Amount": 20.0}]
        xlsx = _build_fake_xlsx({"Purchase": purchase, "Sales": sales})
        slack = _FakeSlackForRead(xlsx)
        store = _make_store_with_pointer(slack)

        rows = store.read_rows("c1", "2026", slack_client=slack, channel_id="C1")
        assert len(rows) == 2
        sheets = {r["_sheet"] for r in rows}
        assert sheets == {"Purchase", "Sales"}

    def test_empty_sheet_yields_no_rows(self):
        xlsx = _build_fake_xlsx({"Purchase": []})
        slack = _FakeSlackForRead(xlsx)
        store = _make_store_with_pointer(slack)

        rows = store.read_rows("c1", "2026", slack_client=slack, channel_id="C1")
        # Empty sheet (no data rows) should yield nothing.
        assert rows == [] or all(
            all(v is None for k, v in r.items() if k != "_sheet") for r in rows
        )


# --------------------------------------------------------------------------- #
# Instruction provider — the question must reach the agent's system prompt
# (regression for: qa_agent only received {"intent":"question"} and replied
#  with a generic capability menu instead of answering).
# --------------------------------------------------------------------------- #


def test_qa_instruction_embeds_question_from_state():
    from accounting_agents.qa_agent import QUESTION_KEY, qa_instruction

    ctx = _FakeToolContext({QUESTION_KEY: "What was my total software spend in FY2026?"})
    text = qa_instruction(ctx)
    assert "What was my total software spend in FY2026?" in text
    assert "Answer THIS question now" in text


def test_qa_instruction_falls_back_without_question():
    from accounting_agents.qa_agent import qa_instruction

    base = qa_instruction(_FakeToolContext({}))
    assert "read-only accounting assistant" in base
    # No question → no "answer this" directive, must not crash, blanks ignored.
    assert "Answer THIS question now" not in base
    assert qa_instruction(_FakeToolContext({"question_text": "   "})) == base
