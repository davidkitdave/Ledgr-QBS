from ledgr_agent.tools.chat_action_tools import explain_tax_treatment_action


def test_explain_tax_is_read_only() -> None:
    result = explain_tax_treatment_action(invoice_number="INV-1")
    assert result["status"] in {"success", "not_found"}
    assert result.get("requires_confirmation") is not True