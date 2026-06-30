"""WS-3.4 — account_flagged propagation to export rows and delivery preview."""

from __future__ import annotations

from ledgr_slack.export.exporters import (
    QbsLedgerExporter,
    collect_account_flagged_summary,
    decorate_preview_account_flags,
    format_account_flagged_note,
)
from ledgr_slack.export.models import InvoiceLine, NormalizedInvoice, PartyInfo
from ledgr_slack.export.tax_classifier import get_tax_classifier


def _flagged_purchase_line(*, flagged: bool, code: str = "6100") -> NormalizedInvoice:
    inv = NormalizedInvoice(
        doc_type="purchase",
        invoice_number="INV-1",
        supplier=PartyInfo(name="Vendor Co"),
        our_gst_registered=True,
    )
    inv.lines.append(
        InvoiceLine(
            description="Consulting",
            net_amount=100.0,
            gst_amount=9.0,
            tax_treatment="SR",
            account_code=code,
            account_flagged=flagged,
            account_flag_reason="low_avg_logprobs" if flagged else None,
        )
    )
    return inv


def test_exporter_rows_carry_account_flagged_metadata():
    exp = QbsLedgerExporter(classifier=get_tax_classifier("sg_gst.yaml"))
    exp.configure_client_context(coa_keys={"6100", "6200"})
    rows = exp.rows([_flagged_purchase_line(flagged=True)], "purchase")
    assert rows[0]["_account_flagged"] is True
    assert rows[0]["_account_flag_reason"] == "low_avg_logprobs"
    assert rows[0]["Account Code / COA"] == "6100"


def test_exporter_rows_confident_line_has_no_flag_metadata():
    exp = QbsLedgerExporter(classifier=get_tax_classifier("sg_gst.yaml"))
    exp.configure_client_context(coa_keys={"6100"})
    rows = exp.rows([_flagged_purchase_line(flagged=False)], "purchase")
    assert "_account_flagged" not in rows[0]


def test_decorate_preview_account_flags_marks_account_column():
    exp = QbsLedgerExporter()
    rows = [
        {
            "Account Code / COA": "6100",
            "_account_flagged": True,
        },
        {
            "Account Code / COA": "6200",
        },
    ]
    decorated = decorate_preview_account_flags(rows, exp, "purchase")
    assert decorated[0]["Account Code / COA"] == "6100 ⚠️"
    assert decorated[1]["Account Code / COA"] == "6200"


def test_collect_and_format_account_flagged_summary():
    batches = [
        {
            "sheet": "Purchase",
            "rows": [
                {"_account_flagged": True, "_account_flag_reason": "narrow_margin"},
                {},
            ],
        }
    ]
    summary = collect_account_flagged_summary(batches)
    assert summary["count"] == 1
    note = format_account_flagged_note(summary)
    assert "⚠️" in note
    assert "1 line has" in note
