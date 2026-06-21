"""Targeted tests for the assistant agent tools and SlackLedgerStore.read_rows.

Covers:
- ``summarize_by_category``: biggest expense category, multi-category totals.
- ``pnl_for_fy``: revenue/expense split, net calculation.
- ``gst_threshold_check``: below / near / above threshold.
- ``bank_totals``: month filter, opening/closing balance behaviour.
- Empty-ledger graceful case for all tools.
- ``SlackLedgerStore.read_rows`` round-trip with a fake Slack workbook.
- The new read-only inspection tools (profile / learned mappings / models).
- The ``assistant_agent`` is a root agent (no ``mode``) with 12 tools.
- ``assistant_instruction`` seeds the client profile from session state.

No live Slack, no live Gemini — all fakes / pure function calls.
"""

from __future__ import annotations

import io
import json

import pytest
from openpyxl import Workbook

from accounting_agents.assistant import (
    LEDGER_DATA_KEY,
    bank_totals,
    gst_threshold_check,
    model_info,
    pnl_for_fy,
    show_client_profile,
    show_learned_mappings,
    summarize_by_category,
)
from accounting_agents.ledger_store import SlackLedgerStore
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


def _bank_rows() -> list[dict]:
    """Two months (Sep + Oct 2025) of bank rows with a B/F opener."""
    return [
        {"Description": "BALANCE B/F", "Withdrawal": None, "Deposit": None,
         "Balance": 1000.0, "Currency": "SGD"},
        {"Date": "05/09/2025", "Description": "FAST PAYMENT", "Withdrawal": 200.0,
         "Deposit": None, "Balance": 800.0, "Currency": "SGD"},
        {"Date": "20/09/2025", "Description": "SALARY", "Withdrawal": None,
         "Deposit": 500.0, "Balance": 1300.0, "Currency": "SGD"},
        {"Date": "03/10/2025", "Description": "RENT", "Withdrawal": 600.0,
         "Deposit": None, "Balance": 700.0, "Currency": "SGD"},
        {"Date": "18/10/2025", "Description": "REFUND", "Withdrawal": None,
         "Deposit": 100.0, "Balance": 800.0, "Currency": "SGD"},
    ]


# --------------------------------------------------------------------------- #
# bank_totals
# --------------------------------------------------------------------------- #


class TestBankTotals:
    def test_no_bank_data_returns_message(self):
        # Invoice rows only → not bank data.
        result = bank_totals(_ctx(_purchase_rows()))
        assert "no bank-statement data" in result.lower()

    def test_all_months_totals(self):
        data = json.loads(bank_totals(_ctx(_bank_rows())))
        assert data["withdrawals"] == pytest.approx(800.0)   # 200 + 600
        assert data["deposits"] == pytest.approx(600.0)      # 500 + 100
        assert data["net"] == pytest.approx(-200.0)
        assert data["transaction_count"] == 4                # B/F excluded
        assert data["opening_balance"] == pytest.approx(1000.0)
        assert data["closing_balance"] == pytest.approx(800.0)
        assert data["currency"] == "SGD"

    def test_month_filter_october(self):
        data = json.loads(bank_totals(_ctx(_bank_rows()), month="October", year="2025"))
        assert data["withdrawals"] == pytest.approx(600.0)   # only RENT
        assert data["deposits"] == pytest.approx(100.0)      # only REFUND
        assert data["transaction_count"] == 2

    def test_month_filter_numeric_and_abbrev(self):
        by_num = json.loads(bank_totals(_ctx(_bank_rows()), month="9"))
        by_abbr = json.loads(bank_totals(_ctx(_bank_rows()), month="Sep"))
        assert by_num["withdrawals"] == pytest.approx(200.0)
        assert by_abbr["withdrawals"] == pytest.approx(200.0)

    def test_month_with_no_rows_reports_none(self):
        data = json.loads(bank_totals(_ctx(_bank_rows()), month="January", year="2025"))
        assert data["transaction_count"] == 0

    def test_filtered_opening_balance_is_period_not_first_bf(self):
        # October's opening should be the balance just before its first txn
        # (Sept closing 1300.0), NOT the sheet's first B/F (1000.0).
        data = json.loads(bank_totals(_ctx(_bank_rows()), month="October", year="2025"))
        assert data["opening_balance"] == pytest.approx(1300.0)
        assert data["closing_balance"] == pytest.approx(800.0)

    def test_unfiltered_opening_balance_is_first_bf(self):
        data = json.loads(bank_totals(_ctx(_bank_rows())))
        assert data["opening_balance"] == pytest.approx(1000.0)


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
# Inspection tools — show_client_profile, show_learned_mappings, model_info
# --------------------------------------------------------------------------- #


class TestShowClientProfile:
    def test_no_profile_loaded_returns_friendly_message(self):
        out = show_client_profile(_FakeToolContext({}))
        assert "no client profile" in out.lower()

    def test_returns_profile_json_with_counts(self):
        state = {
            "client_name": "Acme Pte Ltd",
            "client_uen": "201912345A",
            "region": "SINGAPORE",
            "base_currency": "SGD",
            "tax_registered": True,
            "fye_month": 12,
            "coa": [{"code": "6000"}, {"code": "6100"}, {"code": "6200"}],
            "entity_memory": [{"vendor": "X"}],
        }
        data = json.loads(show_client_profile(_FakeToolContext(state)))
        assert data["client_name"] == "Acme Pte Ltd"
        assert data["client_uen"] == "201912345A"
        assert data["fye_month"] == 12
        assert data["coa_count"] == 3
        assert data["entity_memory_count"] == 1


class TestShowLearnedMappings:
    def test_empty_returns_friendly_message(self):
        out = show_learned_mappings(_FakeToolContext({}))
        assert "no learned mappings" in out.lower()

    def test_returns_both_mappings_when_present(self):
        state = {
            "category_mapping": {"Hotel Booking": "6010"},
            "entity_memory": [{"vendor": "Hotel Booking"}],
        }
        data = json.loads(show_learned_mappings(_FakeToolContext(state)))
        assert data["category_mapping"] == {"Hotel Booking": "6010"}
        assert data["entity_memory"][0]["vendor"] == "Hotel Booking"


class TestModelInfo:
    def test_returns_model_ids_from_config(self):
        from accounting_agents import config

        data = json.loads(model_info(_FakeToolContext({})))
        assert data["chat_model"] == config.MODEL_CHAT
        assert data["model_lite"] == config.MODEL_LITE
        assert data["model_std"] == config.MODEL_STD
        assert data["model_chat"] == config.MODEL_CHAT


# --------------------------------------------------------------------------- #
# Assistant agent shape — root LlmAgent, no mode, exactly 12 tools
# --------------------------------------------------------------------------- #


def test_assistant_agent_is_root_multi_turn():
    """A root LlmAgent carries no ``mode`` (multi-turn default) and exposes
    20 read/explain/inspect + 4 gated write tools.

    See ADR-0008: in ADK 2.2.0 a root agent must not set ``mode``, so the
    runtime uses ``include_contents='default'`` and the agent sees full
    session history. Step 4 (ADR-0009) adds the two gated write tools;
    Step 7 (C-3) adds learn_mapping and replace_recorded_month; Step 7
    (ADR-0010) adds re_extract_document; P1 (2026-06-16) adds the four
    diagnostic / introspection tools; thread follow-up (2026-06-17) adds
    ``lookup_coa_account`` and ``explain_posted_line``.
    """
    from accounting_agents.assistant import assistant_agent

    assert assistant_agent.mode is None
    assert len(assistant_agent.tools) == 24


def test_write_tools_registered_with_confirmation():
    """The two write tools are registered as FunctionTools requiring confirmation."""
    from google.adk.tools import FunctionTool

    from accounting_agents.assistant import assistant_agent

    write_tools = {
        t.func.__name__: t
        for t in assistant_agent.tools
        if isinstance(t, FunctionTool)
        and getattr(t, "func", None) is not None
        and t.func.__name__ in ("amend_ledger_row", "remove_ledger_row")
    }
    assert set(write_tools) == {"amend_ledger_row", "remove_ledger_row"}
    for tool in write_tools.values():
        # Both tools must be gated behind ADK Tool Confirmation. ADK stores the
        # flag privately as ``_require_confirmation``.
        assert tool._require_confirmation is True


def test_thread_before_model_injects_preamble():
    from accounting_agents.assistant import THREAD_FOCUS_KEY, _chat_before_model
    from google.adk.models import LlmRequest
    from google.genai import types

    class _State:
        def get(self, key, default=None):
            data = {
                THREAD_FOCUS_KEY: {
                    "invoice_id": "25-D15",
                    "account_code": "902-A02",
                },
                "thread_delivery_message_ts": "1700000099.000200",
            }
            return data.get(key, default)

    class _Ctx:
        state = _State()

    req = LlmRequest(
        contents=[
            types.Content(
                role="user",
                parts=[types.Part(text="What is the description of the account code?")],
            )
        ]
    )
    result = _chat_before_model(_Ctx(), req)
    assert result is None
    assert req.config.system_instruction
    assert "902-A02" in req.config.system_instruction
    assert "lookup_coa_account" in req.config.system_instruction


def test_assistant_instruction_seeds_profile():
    """P5-slim: profile fields populate the ``{+key+}`` placeholders
    inside ``_BASE_INSTRUCTION`` instead of being prepended as a separate
    preamble block. ADK injects state into the base instruction at LLM
    call time — the rendered prompt is one string, not two concatenated."""
    from accounting_agents.assistant import assistant_instruction

    ctx = _FakeToolContext({
        "client_name": "Acme Pte Ltd",
        "client_uen": "201912345A",
        "region": "SINGAPORE",
        "base_currency": "SGD",
        "tax_registered": True,
        "fye_month": 12,
    })
    text = assistant_instruction(ctx)
    assert "Acme Pte Ltd" in text
    assert "201912345A" in text
    # The rendered prompt still contains the base instruction (with
    # placeholders filled in).
    assert "You are the read-only accounting assistant" in text
    # And the placeholders themselves are gone (ADK substitution worked).
    assert "{+client_name+}" not in text
    # Empty state → optional placeholders collapse to empty strings; the
    # surrounding routing tree + rules still flow. Whitespace-only
    # client_name is treated as absent (no exception, no leftover
    # placeholder text).
    base = assistant_instruction(_FakeToolContext({}))
    assert "You are the read-only accounting assistant" in base
    assert "Acme Pte Ltd" not in base
    # Whitespace-only client_name is treated as absent.
    assert assistant_instruction(_FakeToolContext({"client_name": "   "})) == base


# --------------------------------------------------------------------------- #
# Write tools (Step 4 / C-2) — ADK Tool Confirmation gate (ADR-0009)
# --------------------------------------------------------------------------- #


class _WriteToolContext:
    """ToolContext stub with a recording request_confirmation + settable confirm.

    ``tool_confirmation`` defaults to ``None`` (Turn 1). Set it to a
    ``_FakeConfirmation`` to simulate Turn 2 (the user's answer).
    """

    def __init__(self, state: dict):
        self.state = state
        self.tool_confirmation = None
        self.requested = None  # (hint, payload) recorded on Turn 1

    def request_confirmation(self, *, hint=None, payload=None):
        self.requested = {"hint": hint, "payload": payload}


class _FakeConfirmation:
    def __init__(self, *, confirmed: bool, payload=None, hint=None):
        self.confirmed = confirmed
        self.payload = payload
        self.hint = hint


def _qbs_ledger_rows() -> list[dict]:
    """A QBS-style loaded ledger: a Purchase row + a bank row, with _sheet/_row."""
    return [
        {
            "_sheet": "Purchase", "_row": 2,
            "Invoice Number": "INV-1", "Description": "AWS hosting",
            "Source Amount": 1000.0, "Tax Amount": 90.0,
            "Account Code / COA": "6090", "Doc Type": "P",
        },
        {
            "_sheet": "OCBC - 0001", "_row": 2,
            "Description": "FAST PAYMENT", "Withdrawal": 200.0, "Balance": 800.0,
        },
    ]


def _write_ctx(rows=None, *, tax_registered=True) -> _WriteToolContext:
    return _WriteToolContext(
        {LEDGER_DATA_KEY: rows if rows is not None else _qbs_ledger_rows(),
         "tax_registered": tax_registered}
    )


# ---- amend Turn 1: requests confirmation with before→after diff ----------


def test_amend_turn1_requests_confirmation_account():
    from accounting_agents.assistant import amend_ledger_row

    ctx = _write_ctx()
    out = amend_ledger_row(ctx, row_index="0", field="account", new_value="6010")

    assert "confirm" in out.lower()
    assert ctx.requested is not None
    spec = ctx.requested["payload"]
    assert spec["op"] == "amend"
    assert spec["sheet"] == "Purchase"
    assert spec["row"] == 2
    assert spec["updates"]["Account Code / COA"] == "6010"
    # before→after diff is in the human-readable hint.
    assert "6090" in ctx.requested["hint"]
    assert "6010" in ctx.requested["hint"]


def test_amend_turn1_tax_reclassified_registered_client():
    """A GST-registered client's SR request previews the classifier value (SR tax)."""
    from accounting_agents.assistant import amend_ledger_row

    ctx = _write_ctx(tax_registered=True)
    out = amend_ledger_row(ctx, row_index="0", field="tax", new_value="SR")

    assert "confirm" in out.lower()
    spec = ctx.requested["payload"]
    # SR on a 1000 net @ 9% → Tax Amount 90.0 carried in the write spec.
    assert spec["tax_treatment"] == "SR"
    assert spec["updates"]["Tax Amount"] == 90.0


def test_amend_turn1_tax_forced_nt_for_non_registered_client():
    """NON-registered client: even a user 'SR' request previews/commits NT (master gate)."""
    from accounting_agents.assistant import amend_ledger_row

    ctx = _write_ctx(tax_registered=False)
    out = amend_ledger_row(ctx, row_index="0", field="tax", new_value="SR")

    assert "confirm" in out.lower()
    spec = ctx.requested["payload"]
    assert spec["tax_treatment"] == "NT"
    # NT carries zero tax dollars in the QBS Tax Amount column.
    assert spec["updates"]["Tax Amount"] == 0.0
    # The hint warns the user the treatment was forced.
    assert "NT" in ctx.requested["hint"]


def test_amend_account_reclassifies_tax_to_nt_when_non_registered():
    """Amending a non-tax field on a non-registered client still forces NT tax."""
    from accounting_agents.assistant import amend_ledger_row

    ctx = _write_ctx(tax_registered=False)
    out = amend_ledger_row(ctx, row_index="0", field="account", new_value="6010")  # noqa: F841 — call needed for side-effects on ctx; return unused
    spec = ctx.requested["payload"]
    assert spec["updates"]["Account Code / COA"] == "6010"
    assert spec["tax_treatment"] == "NT"
    assert spec["updates"]["Tax Amount"] == 0.0


# ---- guards: bank sheet, unknown index, disallowed field -----------------


def test_amend_refuses_bank_sheet():
    from accounting_agents.assistant import amend_ledger_row

    ctx = _write_ctx()
    out = amend_ledger_row(ctx, row_index="1", field="account", new_value="6010")
    assert "bank" in out.lower()
    assert ctx.requested is None  # no confirmation requested


def test_remove_refuses_bank_sheet():
    from accounting_agents.assistant import remove_ledger_row

    ctx = _write_ctx()
    out = remove_ledger_row(ctx, row_index="1")
    assert "bank" in out.lower()
    assert ctx.requested is None


def test_amend_unknown_row_index():
    from accounting_agents.assistant import amend_ledger_row

    ctx = _write_ctx()
    out = amend_ledger_row(ctx, row_index="99", field="account", new_value="6010")
    assert "no row" in out.lower() or "lookup_row" in out.lower()
    assert ctx.requested is None


def test_amend_disallowed_field():
    from accounting_agents.assistant import amend_ledger_row

    ctx = _write_ctx()
    out = amend_ledger_row(ctx, row_index="0", field="vendor", new_value="X")
    assert "can't amend" in out.lower() or "editable" in out.lower()
    assert ctx.requested is None


# ---- Turn 2: confirmed=True appends spec; confirmed=False cancels ---------


def test_amend_turn2_confirmed_appends_spec():
    """Turn-2 RE-DERIVES the spec from the original args (no payload echo).

    Mimics the REAL ADK contract: on resume the confirmation has confirmed=True
    but NO payload (ADK does not carry the request-side payload through — see the
    e2e test). The commit must rebuild the canonical spec from row_index/field/
    new_value, identical to the Turn-1 preview by construction.
    """
    from accounting_agents.assistant import PENDING_WRITE_KEY, amend_ledger_row

    ctx = _write_ctx()  # registered client (default)
    ctx.tool_confirmation = _FakeConfirmation(confirmed=True)  # no payload

    out = amend_ledger_row(ctx, row_index="0", field="account", new_value="6010")
    assert "applying" in out.lower() or "confirm" in out.lower()
    pending = ctx.state[PENDING_WRITE_KEY]
    assert len(pending) == 1
    spec = pending[0]
    assert spec["op"] == "amend"
    assert spec["sheet"] == "Purchase"
    assert spec["row"] == 2
    assert spec["updates"]["Account Code / COA"] == "6010"
    # Registered client: the AWS row re-classifies to SR (carries 90.0 tax).
    assert spec["tax_treatment"] == "SR"
    assert spec["updates"]["Tax Amount"] == 90.0
    assert "row_signature" in spec and spec["row_signature"]


def test_amend_turn2_confirmed_forces_nt_for_non_registered():
    """Turn-2 re-derivation re-runs §0.5-C: a non-registered client is forced to NT."""
    from accounting_agents.assistant import PENDING_WRITE_KEY, amend_ledger_row

    ctx = _write_ctx(tax_registered=False)
    ctx.tool_confirmation = _FakeConfirmation(confirmed=True)  # no payload

    # User asks for SR on a non-registered client; the master gate forces NT.
    amend_ledger_row(ctx, row_index="0", field="tax", new_value="SR")
    spec = ctx.state[PENDING_WRITE_KEY][0]
    assert spec["tax_treatment"] == "NT"
    assert spec["updates"]["Tax Amount"] == 0.0


def test_amend_turn2_declined_appends_nothing():
    from accounting_agents.assistant import PENDING_WRITE_KEY, amend_ledger_row

    ctx = _write_ctx()
    ctx.tool_confirmation = _FakeConfirmation(confirmed=False)  # no payload

    out = amend_ledger_row(ctx, row_index="0", field="account", new_value="6010")
    assert "won't change" in out.lower()
    assert ctx.state.get(PENDING_WRITE_KEY) in (None, [])


def test_remove_turn1_requests_confirmation():
    from accounting_agents.assistant import remove_ledger_row

    ctx = _write_ctx()
    out = remove_ledger_row(ctx, row_index="0")
    assert "confirm" in out.lower()
    spec = ctx.requested["payload"]
    assert spec["op"] == "remove"
    assert spec["sheet"] == "Purchase"
    assert spec["row"] == 2
    # HIGH-2: replay-safety signature must be present.
    assert "row_signature" in spec and spec["row_signature"]


def test_remove_turn2_confirmed_appends_spec():
    """Turn-2 re-derives the remove spec from the original row_index (no payload)."""
    from accounting_agents.assistant import PENDING_WRITE_KEY, remove_ledger_row

    ctx = _write_ctx()
    ctx.tool_confirmation = _FakeConfirmation(confirmed=True)  # no payload

    remove_ledger_row(ctx, row_index="0")
    pending = ctx.state[PENDING_WRITE_KEY]
    assert len(pending) == 1
    spec = pending[0]
    assert spec["op"] == "remove"
    assert spec["sheet"] == "Purchase"
    assert spec["row"] == 2
    assert "row_signature" in spec and spec["row_signature"]


def test_remove_turn2_declined_appends_nothing():
    from accounting_agents.assistant import PENDING_WRITE_KEY, remove_ledger_row

    ctx = _write_ctx()
    ctx.tool_confirmation = _FakeConfirmation(confirmed=False)  # no payload

    out = remove_ledger_row(ctx, row_index="0")
    assert "won't remove" in out.lower()
    assert ctx.state.get(PENDING_WRITE_KEY) in (None, [])


# --------------------------------------------------------------------------- #
# HIGH-1 — non-QBS software gate
# --------------------------------------------------------------------------- #


def _xero_ctx() -> _WriteToolContext:
    """A write ctx with Xero software — should be refused before confirmation."""
    rows = [
        {
            "_sheet": "Purchase", "_row": 2,
            "*ContactName": "Acme Trading Pte. Ltd.",
            "Description": "Cloud hosting",
            "*UnitAmount": 500.0, "TaxAmount": 45.0,
            "*AccountCode": "6090", "*TaxType": "SR",
        },
    ]
    return _WriteToolContext(
        {LEDGER_DATA_KEY: rows, "tax_registered": True, "software": "Xero Ledger"}
    )


def test_amend_refuses_non_qbs_software():
    from accounting_agents.assistant import amend_ledger_row

    ctx = _xero_ctx()
    out = amend_ledger_row(ctx, row_index="0", field="account", new_value="6010")
    assert "xero ledger" in out.lower() or "not supported" in out.lower() or "isn't supported" in out.lower()
    assert ctx.requested is None  # no confirmation requested


def test_remove_refuses_non_qbs_software():
    from accounting_agents.assistant import remove_ledger_row

    ctx = _xero_ctx()
    out = remove_ledger_row(ctx, row_index="0")
    assert "not supported" in out.lower() or "isn't supported" in out.lower()
    assert ctx.requested is None


def test_amend_allows_qbs_explicit_software():
    """Explicit 'qbs' (lowercase) is also allowed — used in ledger_store payloads."""
    from accounting_agents.assistant import amend_ledger_row

    ctx = _write_ctx()
    ctx.state["software"] = "qbs"
    out = amend_ledger_row(ctx, row_index="0", field="account", new_value="6010")  # noqa: F841 — call needed for side-effects on ctx; return unused
    assert ctx.requested is not None  # confirmation was requested


def test_amend_allows_missing_software_key():
    """No 'software' key in state → gate passes (QBS is the default)."""
    from accounting_agents.assistant import amend_ledger_row

    ctx = _write_ctx()
    ctx.state.pop("software", None)
    out = amend_ledger_row(ctx, row_index="0", field="account", new_value="6010")  # noqa: F841 — call needed for side-effects on ctx; return unused
    assert ctx.requested is not None


# --------------------------------------------------------------------------- #
# HIGH-2 — row signature in spec
# --------------------------------------------------------------------------- #


def test_amend_turn1_includes_row_signature():
    from accounting_agents.assistant import amend_ledger_row

    ctx = _write_ctx()
    out = amend_ledger_row(ctx, row_index="0", field="account", new_value="6010")  # noqa: F841 — call needed for side-effects on ctx; return unused
    spec = ctx.requested["payload"]
    assert "row_signature" in spec
    assert isinstance(spec["row_signature"], str)
    assert len(spec["row_signature"]) == 16  # sha256 truncated to 16 hex chars


def test_remove_turn1_includes_row_signature():
    from accounting_agents.assistant import remove_ledger_row

    ctx = _write_ctx()
    remove_ledger_row(ctx, row_index="0")
    spec = ctx.requested["payload"]
    assert "row_signature" in spec
    assert spec["row_signature"]


def test_row_signature_differs_for_different_rows():
    from accounting_agents.assistant import _row_signature

    row_a = {"Description": "AWS", "Source Amount": 1000.0, "Account Code / COA": "6090", "Tax Amount": 90.0}
    row_b = {"Description": "Rent", "Source Amount": 2000.0, "Account Code / COA": "6200", "Tax Amount": 0.0}
    assert _row_signature(row_a) != _row_signature(row_b)


def test_row_signature_stable_for_same_row():
    from accounting_agents.assistant import _row_signature

    row = {"Description": "AWS", "Source Amount": 1000.0, "Account Code / COA": "6090", "Tax Amount": 90.0}
    assert _row_signature(row) == _row_signature(dict(row))


# --------------------------------------------------------------------------- #
# learn_mapping tool (Step 7 / C-3)
# --------------------------------------------------------------------------- #


def _learn_ctx(*, coa: list | None = None) -> _FakeToolContext:
    """ToolContext stub for learn_mapping tests."""
    state: dict = {}
    if coa is not None:
        state["coa"] = coa
    return _FakeToolContext(state)


def test_learn_mapping_valid_vendor_and_account_appends_pending():
    """Valid vendor + COA-present account_code appends the right pending entry."""
    from accounting_agents.assistant import PENDING_LEARN_KEY, learn_mapping

    # COA has the code 6090 — should pass validation.
    coa = [{"code": "6090", "name": "Cloud Services"}, {"code": "6200", "name": "Rent"}]
    ctx = _learn_ctx(coa=coa)
    result = learn_mapping(ctx, vendor="Acme Cloud", account_code="6090")

    assert "Acme Cloud" in result
    assert "6090" in result
    pending = ctx.state.get(PENDING_LEARN_KEY)
    assert isinstance(pending, list) and len(pending) == 1
    entry = pending[0]
    assert entry["vendor"] == "Acme Cloud"
    assert entry["account_code"] == "6090"
    assert entry["tax_code"] is None


def test_learn_mapping_valid_vendor_and_tax_code_appends_pending():
    """Valid vendor + tax_code (no account_code) appends correctly."""
    from accounting_agents.assistant import PENDING_LEARN_KEY, learn_mapping

    ctx = _learn_ctx()
    result = learn_mapping(ctx, vendor="Freight Co", tax_code="ZR")

    assert "Freight Co" in result
    assert "ZR" in result
    pending = ctx.state.get(PENDING_LEARN_KEY)
    assert isinstance(pending, list) and len(pending) == 1
    entry = pending[0]
    assert entry["vendor"] == "Freight Co"
    assert entry["account_code"] is None
    assert entry["tax_code"] == "ZR"


def test_learn_mapping_both_account_and_tax_appends_pending():
    """Both account_code and tax_code supplied — both stored."""
    from accounting_agents.assistant import PENDING_LEARN_KEY, learn_mapping

    coa = [{"code": "6090", "name": "Cloud"}]
    ctx = _learn_ctx(coa=coa)
    result = learn_mapping(ctx, vendor="Acme Cloud", account_code="6090", tax_code="SR")

    assert "Acme Cloud" in result
    assert "6090" in result
    assert "SR" in result
    pending = ctx.state[PENDING_LEARN_KEY]
    assert pending[0]["account_code"] == "6090"
    assert pending[0]["tax_code"] == "SR"


def test_learn_mapping_unknown_account_code_rejected():
    """An account_code not in the COA is rejected; nothing is appended."""
    from accounting_agents.assistant import PENDING_LEARN_KEY, learn_mapping

    coa = [{"code": "6090", "name": "Cloud"}, {"code": "6200", "name": "Rent"}]
    ctx = _learn_ctx(coa=coa)
    result = learn_mapping(ctx, vendor="Acme Cloud", account_code="9999")

    assert "9999" in result
    assert "don't recognise" in result.lower() or "not recognise" in result.lower() or "9999" in result
    assert ctx.state.get(PENDING_LEARN_KEY) in (None, [])


def test_learn_mapping_empty_vendor_rejected():
    """Missing vendor returns a helpful message; nothing appended."""
    from accounting_agents.assistant import PENDING_LEARN_KEY, learn_mapping

    ctx = _learn_ctx()
    result = learn_mapping(ctx, vendor="", account_code="6090")

    assert "vendor" in result.lower()
    assert ctx.state.get(PENDING_LEARN_KEY) in (None, [])


def test_learn_mapping_no_codes_rejected():
    """Neither account_code nor tax_code → helpful message, nothing appended."""
    from accounting_agents.assistant import PENDING_LEARN_KEY, learn_mapping

    ctx = _learn_ctx()
    result = learn_mapping(ctx, vendor="Acme Cloud", account_code="", tax_code="")

    assert "account" in result.lower() or "tax" in result.lower()
    assert ctx.state.get(PENDING_LEARN_KEY) in (None, [])


def test_learn_mapping_skips_coa_check_when_coa_empty():
    """When no COA is loaded, any account_code is accepted (can't validate)."""
    from accounting_agents.assistant import PENDING_LEARN_KEY, learn_mapping

    ctx = _learn_ctx(coa=[])  # empty COA list → skip validation
    result = learn_mapping(ctx, vendor="Acme Cloud", account_code="6090")

    assert "Acme Cloud" in result
    pending = ctx.state.get(PENDING_LEARN_KEY)
    assert pending and pending[0]["account_code"] == "6090"


def test_learn_mapping_registered_as_plain_function_not_confirmed():
    """learn_mapping must be registered as a plain function tool (no require_confirmation)."""
    from google.adk.tools import FunctionTool

    from accounting_agents.assistant import assistant_agent

    # Collect all FunctionTool entries that wrap learn_mapping.
    confirmed_learn = [
        t for t in assistant_agent.tools
        if isinstance(t, FunctionTool)
        and getattr(t, "func", None) is not None
        and t.func.__name__ == "learn_mapping"
        and getattr(t, "_require_confirmation", False)
    ]
    assert confirmed_learn == [], (
        "learn_mapping must NOT be registered with require_confirmation=True"
    )

    # Also confirm it IS present in the tools list (either as bare function or
    # as a FunctionTool without require_confirmation).
    from accounting_agents.assistant import learn_mapping as _lm_fn

    tool_fns = set()
    for t in assistant_agent.tools:
        if callable(t) and not isinstance(t, FunctionTool):
            tool_fns.add(t)
        elif isinstance(t, FunctionTool) and getattr(t, "func", None) is not None:
            tool_fns.add(t.func)
    assert _lm_fn in tool_fns, "learn_mapping must be present in assistant_agent.tools"


# --------------------------------------------------------------------------- #
# replace_recorded_month (Step 7 / C-3)
# --------------------------------------------------------------------------- #


class _FakeToolContextWithConfirm:
    """Stub that can simulate Turn-1 (no confirmation) and Turn-2 (confirmed/denied)."""

    def __init__(self, state: dict, *, confirmed: bool | None = None):
        self.state = dict(state)
        self._confirmed = confirmed
        self.confirmation_requested: dict | None = None
        if confirmed is not None:
            self.tool_confirmation = _FakeConfirmation(confirmed=confirmed)
        # no tool_confirmation attr on Turn-1

    def request_confirmation(self, *, hint: str, payload: dict) -> None:
        self.confirmation_requested = {"hint": hint, "payload": payload}


class _FakeConfirmation:
    def __init__(self, *, confirmed: bool):
        self.confirmed = confirmed
        self.payload: dict = {}


def _invoice_rows_two_months() -> list[dict]:
    """Two months (Sep 2025 = 3 rows, Oct 2025 = 1 row) of invoice data."""
    return [
        {"_sheet": "Purchase", "_row": 2, "Date": "05/09/2025",
         "Description": "AWS", "Source Amount": 100.0},
        {"_sheet": "Purchase", "_row": 3, "Date": "20/09/2025",
         "Description": "Zoom", "Source Amount": 50.0},
        {"_sheet": "Sales",    "_row": 2, "Date": "10/09/2025",
         "Description": "Consulting", "Source Amount": 500.0},
        {"_sheet": "Purchase", "_row": 4, "Date": "03/10/2025",
         "Description": "AWS Oct", "Source Amount": 120.0},
    ]


def _qbs_invoice_rows_two_months() -> list[dict]:
    """Same layout as _invoice_rows_two_months but with real QBS export headers."""
    return [
        {"_sheet": "Purchase", "_row": 2, "Invoice Date": "05/09/2025",
         "Invoice Number": "INV-P1", "Description": "AWS", "Source Amount": 100.0},
        {"_sheet": "Purchase", "_row": 3, "Invoice Date": "20/09/2025",
         "Invoice Number": "INV-P2", "Description": "Zoom", "Source Amount": 50.0},
        {"_sheet": "Sales", "_row": 2, "Invoice Date": "10/09/2025",
         "Invoice Number": "INV-S1", "Description": "Consulting", "Source Amount": 500.0},
        {"_sheet": "Purchase", "_row": 4, "Invoice Date": "03/10/2025",
         "Invoice Number": "INV-P3", "Description": "AWS Oct", "Source Amount": 120.0},
    ]


from accounting_agents.assistant import replace_recorded_month, PENDING_WRITE_KEY


class TestReplaceRecordedMonth:
    def _state(self, rows=None, software="QBS Ledger", fy="2026"):
        return {
            LEDGER_DATA_KEY: rows if rows is not None else _invoice_rows_two_months(),
            "software": software,
            "fy": fy,
        }

    def test_software_gate_refuses_non_qbs(self):
        ctx = _FakeToolContextWithConfirm(self._state(software="Xero"))
        result = replace_recorded_month(ctx, "September 2025")
        assert "isn't supported" in result

    def test_ledger_not_loaded_gate(self):
        ctx = _FakeToolContextWithConfirm({LEDGER_DATA_KEY: [], "fy": "2026"})
        result = replace_recorded_month(ctx, "September 2025")
        assert "not loaded" in result.lower()

    def test_turn1_counts_rows_and_writes_nothing(self):
        ctx = _FakeToolContextWithConfirm(self._state())
        replace_recorded_month(ctx, "September 2025")
        # Should have requested confirmation.
        assert ctx.confirmation_requested is not None
        hint = ctx.confirmation_requested["hint"]
        assert "2 Purchase" in hint or "Purchase" in hint
        assert "1 Sales" in hint or "Sales" in hint
        assert "September 2025" in hint
        # Nothing written to state.
        assert PENDING_WRITE_KEY not in ctx.state

    def test_turn1_counts_qbs_invoice_date_rows(self):
        """QBS workbooks use 'Invoice Date', not 'Date' — must still count rows."""
        ctx = _FakeToolContextWithConfirm(
            self._state(rows=_qbs_invoice_rows_two_months())
        )
        replace_recorded_month(ctx, "September 2025")
        assert ctx.confirmation_requested is not None
        hint = ctx.confirmation_requested["hint"]
        assert "2 Purchase" in hint
        assert "1 Sales" in hint

    def test_turn1_no_match_returns_message(self):
        ctx = _FakeToolContextWithConfirm(self._state())
        result = replace_recorded_month(ctx, "January 2025")
        assert ctx.confirmation_requested is None
        assert "don't see" in result.lower() or "nothing to clear" in result.lower()
        assert PENDING_WRITE_KEY not in ctx.state

    def test_turn2_confirmed_appends_spec(self):
        ctx = _FakeToolContextWithConfirm(self._state(), confirmed=True)
        replace_recorded_month(ctx, "September 2025")
        pending = ctx.state.get(PENDING_WRITE_KEY)
        assert isinstance(pending, list) and len(pending) == 1
        spec = pending[0]
        assert spec["op"] == "replace_month"
        assert spec["year"] == 2025
        assert spec["month"] == 9

    def test_turn2_denied_writes_nothing(self):
        ctx = _FakeToolContextWithConfirm(self._state(), confirmed=False)
        result = replace_recorded_month(ctx, "September 2025")
        assert "won't clear" in result.lower() or "won't" in result.lower()
        assert PENDING_WRITE_KEY not in ctx.state

    # Month-parser coverage

    def test_parse_month_name_full(self):
        ctx = _FakeToolContextWithConfirm(self._state(), confirmed=True)
        replace_recorded_month(ctx, "September 2025")
        spec = ctx.state[PENDING_WRITE_KEY][0]
        assert spec["month"] == 9 and spec["year"] == 2025

    def test_parse_month_name_abbrev(self):
        ctx = _FakeToolContextWithConfirm(self._state(), confirmed=True)
        replace_recorded_month(ctx, "Sep 2025")
        spec = ctx.state[PENDING_WRITE_KEY][0]
        assert spec["month"] == 9 and spec["year"] == 2025

    def test_parse_iso_format(self):
        ctx = _FakeToolContextWithConfirm(self._state(), confirmed=True)
        replace_recorded_month(ctx, "2025-09")
        spec = ctx.state[PENDING_WRITE_KEY][0]
        assert spec["month"] == 9 and spec["year"] == 2025

    def test_parse_slash_format(self):
        ctx = _FakeToolContextWithConfirm(self._state(), confirmed=True)
        replace_recorded_month(ctx, "09/2025")
        spec = ctx.state[PENDING_WRITE_KEY][0]
        assert spec["month"] == 9 and spec["year"] == 2025

    def test_parse_bare_name_infers_year_from_fy(self):
        """Month name without year → year inferred from state["fy"]."""
        ctx = _FakeToolContextWithConfirm(
            {LEDGER_DATA_KEY: _invoice_rows_two_months(), "software": "QBS Ledger", "fy": "2025"},
            confirmed=True,
        )
        replace_recorded_month(ctx, "September")
        spec = ctx.state[PENDING_WRITE_KEY][0]
        assert spec["month"] == 9 and spec["year"] == 2025

    def test_bad_month_string_returns_error(self):
        ctx = _FakeToolContextWithConfirm(self._state())
        result = replace_recorded_month(ctx, "NotAMonth")
        assert "couldn't parse" in result.lower() or "parse" in result.lower()
        assert ctx.confirmation_requested is None


# --------------------------------------------------------------------------- #
# re_extract_document (Step 7 / ADR-0010) — gated re-read + replace
# --------------------------------------------------------------------------- #


def _reextract_ctx(*, software=None) -> _WriteToolContext:
    """A write ctx for re_extract_document. ``software`` seeds the gate; the
    ledger rows are irrelevant (the tool only needs file_id + hints)."""
    state = {LEDGER_DATA_KEY: _qbs_ledger_rows(), "tax_registered": True}
    if software is not None:
        state["software"] = software
    return _WriteToolContext(state)


def test_reextract_refuses_non_qbs_software():
    """The software gate refuses a non-QBS workbook before any confirmation."""
    from accounting_agents.assistant import PENDING_REEXTRACT_KEY, re_extract_document

    ctx = _reextract_ctx(software="Xero Ledger")
    out = re_extract_document(ctx, file_id="F1", hints="read as a credit note")
    assert "not supported" in out.lower() or "isn't supported" in out.lower()
    assert ctx.requested is None  # no confirmation requested
    assert not ctx.state.get(PENDING_REEXTRACT_KEY)


def test_reextract_missing_file_id_is_helpful_and_writes_nothing():
    from accounting_agents.assistant import PENDING_REEXTRACT_KEY, re_extract_document

    ctx = _reextract_ctx()
    out = re_extract_document(ctx, file_id="   ", hints="read as a credit note")
    assert "file id" in out.lower()
    assert ctx.requested is None
    assert not ctx.state.get(PENDING_REEXTRACT_KEY)


def test_reextract_missing_hints_is_helpful_and_writes_nothing():
    from accounting_agents.assistant import PENDING_REEXTRACT_KEY, re_extract_document

    ctx = _reextract_ctx()
    out = re_extract_document(ctx, file_id="F1", hints="   ")
    # The hint is the whole point — refuse and explain.
    assert "how to re-read" in out.lower() or "hint" in out.lower()
    assert ctx.requested is None
    assert not ctx.state.get(PENDING_REEXTRACT_KEY)


def test_reextract_turn1_previews_with_identity_caveat_and_writes_nothing():
    from accounting_agents.assistant import PENDING_REEXTRACT_KEY, re_extract_document

    ctx = _reextract_ctx()
    out = re_extract_document(ctx, file_id="F1", hints="read as a credit note")

    assert "confirmation" in out.lower() or "yes" in out.lower()
    assert ctx.requested is not None
    hint = ctx.requested["hint"]
    # Honest per ADR-0010: states the identity-change caveat + the clear fallback.
    assert "F1" in hint
    assert "read as a credit note" in hint
    assert "credit note" in hint.lower()
    assert "clear" in hint.lower()
    # Turn-1 writes nothing.
    assert not ctx.state.get(PENDING_REEXTRACT_KEY)
    # The preview payload carries the deterministic spec for audit.
    assert ctx.requested["payload"]["op"] == "reextract"
    assert ctx.requested["payload"]["file_id"] == "F1"


def test_reextract_turn2_confirmed_appends_spec():
    from accounting_agents.assistant import PENDING_REEXTRACT_KEY, re_extract_document

    ctx = _reextract_ctx()
    ctx.tool_confirmation = _FakeConfirmation(confirmed=True)  # no payload
    out = re_extract_document(ctx, file_id="F1", hints="zero-rate the freight line")

    assert "confirmed" in out.lower()
    pending = ctx.state[PENDING_REEXTRACT_KEY]
    assert pending == [
        {"op": "reextract", "file_id": "F1", "hints": "zero-rate the freight line"}
    ]


def test_reextract_turn2_declined_writes_nothing():
    from accounting_agents.assistant import PENDING_REEXTRACT_KEY, re_extract_document

    ctx = _reextract_ctx()
    ctx.tool_confirmation = _FakeConfirmation(confirmed=False)
    out = re_extract_document(ctx, file_id="F1", hints="read as a credit note")

    assert "won't" in out.lower() or "okay" in out.lower()
    assert not ctx.state.get(PENDING_REEXTRACT_KEY)


def test_reextract_registered_in_tool_list_with_confirmation():
    """re_extract_document is registered as a FunctionTool requiring confirmation."""
    from google.adk.tools import FunctionTool

    from accounting_agents.assistant import assistant_agent

    tool = next(
        t for t in assistant_agent.tools
        if isinstance(t, FunctionTool)
        and getattr(t, "func", None) is not None
        and t.func.__name__ == "re_extract_document"
    )
    assert tool._require_confirmation is True


def test_explain_document_processing_soa_legacy_path():
    from accounting_agents.assistant import PROCESSING_LOG_KEY, explain_document_processing

    ctx = _FakeToolContext({
        PROCESSING_LOG_KEY: [
            {
                "file_id": "F-SOA-1",
                "filename": "vendor_soa.pdf",
                "doc_type": "statement_of_account",
                "extraction_path": "legacy",
                "soa_legacy_path": True,
                "row_count": 12,
                "delivered_at": "2026-06-16T12:00:00+00:00",
                "fy": "2025",
            }
        ]
    })
    raw = explain_document_processing(ctx, filename="vendor_soa.pdf")
    data = json.loads(raw)
    assert data["extraction_path"] == "legacy"
    assert data["doc_type"] == "statement_of_account"
    assert "legacy DocumentRecord" in data["summary"]


def test_explain_document_processing_understand_path():
    from accounting_agents.assistant import PROCESSING_LOG_KEY, explain_document_processing

    ctx = _FakeToolContext({
        PROCESSING_LOG_KEY: [
            {
                "file_id": "F-INV-1",
                "filename": "invoice.pdf",
                "doc_type": "invoice",
                "extraction_path": "understand",
                "soa_legacy_path": False,
                "row_count": 2,
                "delivered_at": "2026-06-16T13:00:00+00:00",
                "fy": "2025",
            }
        ]
    })
    raw = explain_document_processing(ctx)
    data = json.loads(raw)
    assert data["extraction_path"] == "understand"
    assert "Understand single-call" in data["summary"]


def test_list_recent_documents_enriched_with_processing_log():
    from accounting_agents.assistant import PROCESSING_LOG_KEY, list_recent_documents

    rows = [
        {
            "Date": "01/12/2025",
            "Source Filename": "invoice.pdf",
            "Doc Type": "P",
            "Source Amount": 100.0,
        }
    ]
    ctx = _FakeToolContext({
        LEDGER_DATA_KEY: rows,
        PROCESSING_LOG_KEY: [
            {
                "filename": "invoice.pdf",
                "file_id": "F-INV-1",
                "doc_type": "invoice",
                "extraction_path": "understand",
            }
        ],
    })
    data = json.loads(list_recent_documents(ctx))
    doc = data["documents"][0]
    assert doc["extraction_path"] == "understand"
    assert doc["file_id"] == "F-INV-1"


# --------------------------------------------------------------------------- #
# P0-2 — Xero → tool column normalization
# --------------------------------------------------------------------------- #


def test_xero_row_normalization_aliases_to_qbs_columns():
    """Xero rows (``*InvoiceNumber``, ``*InvoiceDate`` etc.) should surface as
    QBS column names so chat tools that read ``Source Filename`` /
    ``Description`` / ``Source Amount`` see the right values."""
    from accounting_agents.assistant import _normalize_row_for_tools

    raw = {
        "_sheet": "Purchase",
        "*InvoiceNumber": "INV-9001",
        "*InvoiceDate": "2025-09-12",
        "*Description": "Office supplies",
        "*AccountCode": "6100-Software",
        "*UnitAmount": 123.45,
    }
    out = _normalize_row_for_tools(raw)
    assert out["Source Filename"] == "Xero:INV-9001"
    assert out["Doc Type"] == "Purchase"
    assert out["Source Amount"] == 123.45
    assert out["Description"] == "Office supplies"
    assert out["Account Code / COA"] == "6100-Software"
    assert out["Date"] == "2025-09-12"
    # Original Xero keys are still there for callers that want them.
    assert out["*InvoiceNumber"] == "INV-9001"


def test_qbs_row_normalization_preserves_existing_fields():
    """QBS rows already use the canonical column names; normalization must
    not overwrite them or lose data."""
    from accounting_agents.assistant import _normalize_row_for_tools

    raw = {
        "_sheet": "Purchase",
        "Source Filename": "acme.pdf",
        "Doc Type": "Purchase",
        "Source Amount": 200.0,
        "Description": "Existing QBS row",
        "Account Code / COA": "6000",
        "Date": "01/10/2025",
    }
    out = _normalize_row_for_tools(raw)
    assert out["Source Filename"] == "acme.pdf"
    assert out["Source Amount"] == 200.0
    assert out["Description"] == "Existing QBS row"


def test_get_rows_applies_normalization_to_every_row():
    """``_get_rows`` should always return normalized rows so downstream tools
    can rely on QBS column names regardless of the source software."""
    from accounting_agents.assistant import _get_rows

    raw = [
        {"_sheet": "Purchase", "*InvoiceNumber": "X-1", "*UnitAmount": 50.0},
        {"_sheet": "Sales", "*InvoiceNumber": "X-2", "*UnitAmount": 75.0},
    ]
    rows = _get_rows(_FakeToolContext({LEDGER_DATA_KEY: raw}))
    assert all("Source Filename" in r for r in rows)
    assert rows[0]["Doc Type"] == "Purchase"
    assert rows[1]["Doc Type"] == "Sales"
    assert [r["Source Amount"] for r in rows] == [50.0, 75.0]


def test_list_recent_documents_empty_returns_diagnostic_message():
    """Empty-state must include FY/row count/processing-log depth, not the
    generic "upload the ledger" string."""
    from accounting_agents.assistant import (
        PROCESSING_LOG_KEY,
        list_recent_documents,
    )

    ctx = _FakeToolContext({
        LEDGER_DATA_KEY: [],
        "fy_loaded": "2026",
        "ledger_row_count": 0,
        "fy_pointers": [
            {"fy": "2025", "row_count": 42, "has_data": True},
            {"fy": "2026", "row_count": 0, "has_data": False},
        ],
        PROCESSING_LOG_KEY: [
            {"filename": "old.pdf", "file_id": "F-1", "extraction_path": "understand"},
        ],
    })
    msg = list_recent_documents(ctx)
    assert "FY2026" in msg
    assert "FY2025=42" in msg
    assert "row_count=0" in msg
    assert "Processing log has 1 entries" in msg
    # Also assert the legacy "not loaded" wording is preserved so the older
    # tests that check for it still match.
    assert "not loaded" in msg.lower()


# --------------------------------------------------------------------------- #
# P1 — Diagnostic / introspection tools
# --------------------------------------------------------------------------- #


def test_diagnose_assistant_context_empty_ledger():
    """diagnose_assistant_context returns the FY pointers and counts even
    when the ledger is empty, so the LLM can answer 'what FYs exist?'."""
    from accounting_agents.assistant import (
        PENDING_REVIEWS_KEY,
        PROCESSING_LOG_KEY,
        diagnose_assistant_context,
    )

    ctx = _FakeToolContext({
        "client_name": "Company-A",
        "software": "Xero Ledger",
        "fy_loaded": "2026",
        "ledger_row_count": 0,
        "fy_pointers": [
            {"fy": "2025", "row_count": 42, "has_data": True},
            {"fy": "2026", "row_count": 0, "has_data": False},
        ],
        PROCESSING_LOG_KEY: [{"filename": "a.pdf"}],
        PENDING_REVIEWS_KEY: [],
    })
    data = json.loads(diagnose_assistant_context(ctx))
    assert data["status"] == "success"
    assert data["client_name"] == "Company-A"
    assert data["software"] == "Xero Ledger"
    assert data["fy_loaded"] == "2026"
    assert data["ledger_row_count"] == 0
    assert data["ledger_type"] == "empty"
    assert data["processing_log_count"] == 1
    assert data["pending_review_count"] == 0
    assert data["onboarding_required"] is False
    assert len(data["fy_pointers"]) == 2


def test_diagnose_assistant_context_detects_bank_ledger():
    """ledger_type='bank' when any sample row is on a non-invoice sheet."""
    from accounting_agents.assistant import diagnose_assistant_context

    ctx = _FakeToolContext({
        "client_name": "Acme",
        "software": "QBS",
        "fy_loaded": "2025",
        "ledger_row_count": 1,
        LEDGER_DATA_KEY: [
            {"_sheet": "OCBC - 0001", "Date": "01/01/2025", "Balance": 100.0},
        ],
    })
    data = json.loads(diagnose_assistant_context(ctx))
    assert data["ledger_type"] == "bank"


def test_diagnose_assistant_context_detects_invoice_ledger():
    """ledger_type='invoice' for Purchase/Sales sheets."""
    from accounting_agents.assistant import diagnose_assistant_context

    ctx = _FakeToolContext({
        "client_name": "Acme",
        "software": "QBS",
        "fy_loaded": "2025",
        "ledger_row_count": 1,
        LEDGER_DATA_KEY: [
            {"_sheet": "Purchase", "Date": "01/01/2025", "Source Amount": 50.0},
        ],
    })
    data = json.loads(diagnose_assistant_context(ctx))
    assert data["ledger_type"] == "invoice"


def test_list_processing_history_returns_recent_entries():
    from accounting_agents.assistant import (
        PROCESSING_LOG_KEY,
        list_processing_history,
    )

    ctx = _FakeToolContext({
        PROCESSING_LOG_KEY: [
            {
                "filename": "a.pdf",
                "file_id": "F-1",
                "doc_type": "invoice",
                "extraction_path": "understand",
                "row_count": 3,
                "fy": "2025",
            },
            {
                "filename": "b.pdf",
                "file_id": "F-2",
                "doc_type": "statement_of_account",
                "extraction_path": "soa_legacy",
                "soa_legacy_path": True,
                "row_count": 10,
            },
        ],
    })
    data = json.loads(list_processing_history(ctx))
    assert len(data["entries"]) == 2
    assert data["entries"][0]["filename"] == "a.pdf"
    assert data["entries"][1]["soa_legacy_path"] is True


def test_list_processing_history_empty_log():
    from accounting_agents.assistant import list_processing_history

    data = json.loads(list_processing_history(_FakeToolContext({})))
    assert data == {"entries": []}


def test_get_document_processing_detail_merges_session_snapshot():
    """Detail tool layers the read-only ``document_sessions`` snapshot on
    top of the processing log entry so the LLM can cite both."""
    from accounting_agents.assistant import (
        DOCUMENT_SESSIONS_KEY,
        PROCESSING_LOG_KEY,
        get_document_processing_detail,
    )

    ctx = _FakeToolContext({
        PROCESSING_LOG_KEY: [
            {
                "filename": "invoice.pdf",
                "file_id": "F-INV-1",
                "doc_type": "invoice",
                "extraction_path": "understand",
            }
        ],
        DOCUMENT_SESSIONS_KEY: {
            "F-INV-1": {
                "doc_type": "invoice",
                "extraction_path": "understand",
                "review_reasons": ["tax_code_unknown"],
                "source_filename": "invoice.pdf",
                "summary_table_size": 5,
                "normalized_invoice_count": 1,
            }
        },
    })
    data = json.loads(
        get_document_processing_detail(ctx, file_id="F-INV-1")
    )
    assert data["file_id"] == "F-INV-1"
    assert data["review_reasons"] == ["tax_code_unknown"]
    assert data["summary_table_size"] == 5
    assert data["normalized_invoice_count"] == 1


def test_get_document_processing_detail_partial_filename_match():
    """Users say ``25-D15``; log stores ``25-D15-Company-A.pdf``."""
    from accounting_agents.assistant import (
        PROCESSING_LOG_KEY,
        get_document_processing_detail,
    )

    ctx = _FakeToolContext({
        PROCESSING_LOG_KEY: [
            {
                "filename": "25-D15-Company-A.pdf",
                "file_id": "F-D15",
                "doc_type": "invoice",
                "extraction_path": "understand",
            }
        ],
    })
    data = json.loads(
        get_document_processing_detail(ctx, filename="25-D15")
    )
    assert data["file_id"] == "F-D15"
    assert "25-D15" in data["filename"]


def test_lookup_row_finds_xero_invoice_number():
    from accounting_agents.assistant import LEDGER_DATA_KEY, lookup_row

    ctx = _FakeToolContext({
        LEDGER_DATA_KEY: [
            {
                "_sheet": "Purchase",
                "*InvoiceNumber": "25-D12",
                "*ContactName": "Person-1",
                "*Description": "Professional services",
                "*AccountCode": "6-3000",
                "*UnitAmount": "500.00",
            }
        ],
    })
    data = json.loads(lookup_row(ctx, query="25-D12"))
    assert len(data["matches"]) == 1
    assert data["matches"][0]["account_code"] == "6-3000"
    assert "Person-1" in (data["matches"][0].get("vendor") or "")


def test_lookup_row_falls_back_to_processing_log_when_ledger_empty():
    from accounting_agents.assistant import PROCESSING_LOG_KEY, lookup_row

    ctx = _FakeToolContext({
        "ledger_data": [],
        "fy_loaded": "2026",
        PROCESSING_LOG_KEY: [
            {
                "filename": "25-D12-Company-A.pdf",
                "file_id": "F-D12",
                "fy": "2025",
                "doc_type": "invoice",
            }
        ],
    })
    data = json.loads(lookup_row(ctx, query="25-D12"))
    assert data["matches"] == []
    assert len(data["processing_log_matches"]) == 1
    assert data["processing_log_matches"][0]["fy"] == "2025"


def test_explain_categorization_accepts_row_index():
    from accounting_agents.assistant import LEDGER_DATA_KEY, explain_categorization

    ctx = _FakeToolContext({
        LEDGER_DATA_KEY: [
            {
                "Vendor": "Acme Pte Ltd",
                "Description": "Consulting fees",
            }
        ],
        "coa": [{"code": "6100", "name": "Professional Fees"}],
    })
    data = json.loads(explain_categorization(ctx, row_index="0"))
    assert data["status"] in ("resolved", "unresolved")
    assert "account_code" in data


def test_get_document_processing_detail_not_found_lists_recent():
    from accounting_agents.assistant import (
        PROCESSING_LOG_KEY,
        get_document_processing_detail,
    )

    ctx = _FakeToolContext({
        PROCESSING_LOG_KEY: [
            {"filename": "old.pdf", "file_id": "F-OLD", "extraction_path": "understand"},
        ],
    })
    data = json.loads(
        get_document_processing_detail(ctx, filename="missing.pdf")
    )
    assert data["status"] == "not_found"
    assert data["recent"][0]["filename"] == "old.pdf"


def test_list_pending_reviews_returns_interrupts():
    from accounting_agents.assistant import (
        PENDING_REVIEWS_KEY,
        list_pending_reviews,
    )

    ctx = _FakeToolContext({
        PENDING_REVIEWS_KEY: [
            {
                "interrupt_id": "INT-1",
                "file_id": "F-1",
                "filename": "x.pdf",
                "doc_type": "invoice",
                "asked_at": "2026-06-17T01:00:00Z",
                "reason": "tax_code_unknown",
                "options": ["approve", "edit", "reject"],
            }
        ],
    })
    data = json.loads(list_pending_reviews(ctx))
    assert data["count"] == 1
    assert data["reviews"][0]["interrupt_id"] == "INT-1"
    assert data["reviews"][0]["options"] == ["approve", "edit", "reject"]


def test_list_pending_reviews_empty():
    from accounting_agents.assistant import list_pending_reviews

    data = json.loads(list_pending_reviews(_FakeToolContext({})))
    assert data == {"reviews": [], "count": 0}


def test_lookup_coa_account_finds_code():
    from accounting_agents.assistant import lookup_coa_account

    ctx = _FakeToolContext({
        "coa": [
            {"code": "902-A02", "description": "Professional Fees", "account_type": "Expense"},
        ],
    })
    data = json.loads(lookup_coa_account(ctx, account_code="902-A02"))
    assert data["status"] == "found"
    assert data["description"] == "Professional Fees"
    assert data["account_type"] == "Expense"


def test_lookup_coa_account_uses_thread_focus():
    from accounting_agents.assistant import THREAD_FOCUS_KEY, lookup_coa_account

    ctx = _FakeToolContext({
        THREAD_FOCUS_KEY: {"account_code": "902-A02"},
        "coa": [{"code": "902-A02", "description": "Professional Fees"}],
    })
    data = json.loads(lookup_coa_account(ctx, account_code=""))
    assert data["status"] == "found"
    assert data["code"] == "902-A02"


def test_explain_posted_line_combines_ledger_and_coa():
    from accounting_agents.assistant import LEDGER_DATA_KEY, explain_posted_line

    ctx = _FakeToolContext({
        LEDGER_DATA_KEY: [
            {
                "*InvoiceNumber": "25-D15",
                "*ContactName": "Person-1",
                "*Description": "Consulting fees",
                "*AccountCode": "902-A02",
            }
        ],
        "coa": [{"code": "902-A02", "description": "Professional Fees"}],
        "processing_log": [
            {
                "filename": "25-D15-Company-A.pdf",
                "file_id": "F-D15",
                "invoice_ids": ["25-D15"],
                "extraction_path": "understand",
            }
        ],
    })
    data = json.loads(explain_posted_line(ctx, invoice_id="25-D15"))
    assert data["status"] == "found"
    assert data["posted_account_code"] == "902-A02"
    assert data["coa_description"] == "Professional Fees"
    assert data["vendor"] == "Person-1"


def test_diagnostic_tools_registered_on_assistant_agent():
    """Introspection + COA tools must be wired up to ``assistant_agent``."""

    from accounting_agents.assistant import assistant_agent

    tool_names = {
        getattr(t, "func", t).__name__
        for t in assistant_agent.tools
    }
    for name in (
        "diagnose_assistant_context",
        "get_document_processing_detail",
        "list_processing_history",
        "list_pending_reviews",
        "lookup_coa_account",
        "explain_posted_line",
    ):
        assert name in tool_names, f"{name!r} missing from assistant_agent.tools"


def test_assistant_instruction_includes_diagnostic_counts_in_preamble():
    """P3: preamble must include FY/row count/processing-log/pending-review."""
    from accounting_agents.assistant import (
        PENDING_REVIEWS_KEY,
        PROCESSING_LOG_KEY,
        assistant_instruction,
    )

    class _Ctx:
        state = {
            "client_name": "Acme",
            "client_uen": "UEN-1",
            "region": "SINGAPORE",
            "base_currency": "SGD",
            "tax_registered": True,
            "fye_month": 12,
            "fy_loaded": "2025",
            "ledger_row_count": 42,
            PROCESSING_LOG_KEY: [
                {"filename": "a.pdf"},
                {"filename": "b.pdf"},
            ],
            PENDING_REVIEWS_KEY: [
                {"interrupt_id": "INT-1"},
            ],
        }

    inst = assistant_instruction(_Ctx())
    # The preamble should name the loaded FY, the row count, the
    # processing-log depth, and the pending-review count so the model
    # knows context BEFORE picking a tool.
    assert "Acme" in inst
    assert "FY2025" in inst
    assert "42 rows" in inst
    assert "Processing history: 2 deliveries" in inst
    assert "Pending reviews: 1" in inst


def test_base_instruction_has_diagnostic_routing_decision_tree():
    """P3: the base instruction should include explicit routing guidelines."""
    from accounting_agents.assistant import _BASE_INSTRUCTION

    assert "diagnose_assistant_context" in _BASE_INSTRUCTION
    assert "Routing guidelines:" in _BASE_INSTRUCTION
    assert "lookup_row" in _BASE_INSTRUCTION
    assert "lookup_coa_account" in _BASE_INSTRUCTION

