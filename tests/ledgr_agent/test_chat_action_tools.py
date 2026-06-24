import json

from ledgr_agent.tools.chat_action_tools import explain_tax_treatment_action


class _StubContext:
    state: dict = {}


def test_explain_tax_is_read_only() -> None:
    raw = explain_tax_treatment_action(_StubContext(), line_description="INV-1")
    result = json.loads(raw) if isinstance(raw, str) else raw
    # Thin re-export returns the upstream JSON payload unchanged.
    assert "tax_treatment" in result or "tax_jurisdiction" in result
    assert result.get("requires_confirmation") is not True