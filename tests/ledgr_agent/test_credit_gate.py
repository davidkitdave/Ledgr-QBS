from ledgr_agent.tools.document_tools import process_document_batch


def test_zero_balance_blocks_before_engine(tmp_path, monkeypatch) -> None:
    invoice_p = tmp_path / "invoice.pdf"
    invoice_p.write_bytes(b"%PDF stub")

    class _Gate:
        def check(self, **_kw):
            return {"allowed": False, "reason": "zero_credit", "balance": 0}

    monkeypatch.setattr(
        "ledgr_agent.tools.document_tools._credit_gate",
        lambda **_kw: _Gate().check(),
    )

    result = process_document_batch(None, paths=[str(invoice_p)])

    assert result["status"] == "blocked"
    assert result["validation_summary"]["block_reason"] == "zero_credit"
    assert result["llm_call_count"] == 0
