"""Regression: BookingProposal must serialize for Gemini structured output."""

from invoice_processing.extract.book import BookingProposal


def test_booking_proposal_json_schema_is_defined():
    """Guards against missing Literal import breaking response_schema."""
    schema = BookingProposal.model_json_schema()
    assert "direction_for_client" in schema.get("properties", {})
    props = schema["properties"]["direction_for_client"]
    assert "enum" in props or "anyOf" in props or props.get("type") == "string"


def test_booking_proposal_validates_minimal_payload():
    proposal = BookingProposal.model_validate(
        {
            "doc_kind": "expense_claim",
            "direction_for_client": "purchase",
            "direction_reason": "Client is bill-to on form",
            "ledger_lines": [{"description": "Travel", "net_amount": 100.0}],
            "document_total": 100.0,
            "tax_visible_on_document": False,
        }
    )
    assert proposal.direction_for_client == "purchase"
