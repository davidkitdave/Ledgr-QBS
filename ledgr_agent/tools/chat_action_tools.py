from __future__ import annotations

import json

from google.adk.tools import FunctionTool, ToolContext

from accounting_agents.assistant.tools.explain_tools import explain_tax_treatment
from accounting_agents.assistant.tools.mutate_tools import amend_ledger_row


def explain_tax_treatment_action(
    tool_context: ToolContext | None = None,
    invoice_number: str = "",
    line_description: str = "",
    **kwargs: object,
) -> dict[str, object]:
    """Read-only chat action: explain the tax treatment the LLM reasoner would pick.

    Thin wrapper around ``accounting_agents.assistant.tools.explain_tools.explain_tax_treatment``.
    Re-exported under an ``_action`` suffix so the clean root agent advertises the
    mutating/read-only split clearly to the model. Accepts either
    ``invoice_number`` (chat-style label) or ``line_description`` (engine-style
    label) for ergonomics.
    """
    description = (line_description or invoice_number or "").strip()
    if tool_context is None:

        class _EmptyContext:
            state: dict[str, object] = {}

        ctx: ToolContext = _EmptyContext()  # type: ignore[assignment]
    else:
        ctx = tool_context

    raw = explain_tax_treatment(
        ctx,
        line_description=description,
        **{k: v for k, v in kwargs.items() if k in {
            "tax_keyword",
            "net_amount",
            "gst_amount",
            "doc_type",
            "invoice_date",
            "our_gst_registered",
        }},
    )
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {"status": "success", "raw": raw}
        return {"status": "success", **payload}
    if isinstance(raw, dict):
        return {"status": "success", **raw}
    return {"status": "not_found"}


amend_ledger_row_action = FunctionTool(amend_ledger_row, require_confirmation=True)