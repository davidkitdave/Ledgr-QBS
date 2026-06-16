"""Hermetic tests for the 6 batch direction fixtures.

These tests exercise the Drive-parity direction logic (the
``_resolve_direction_from_extract`` helper) against captured party
information for six sample batch documents. They run without any API calls
because they feed pre-extracted ``DocumentLedgerExtract`` objects through the
direction resolver, then assert against the fixture's expected verdict.

The fixtures live in ``eval/fixtures/direction/`` (JSON only, no PDFs).
"""

from __future__ import annotations

import json
from pathlib import Path

from accounting_agents.nodes import _resolve_direction_from_extract
from invoice_processing.extract.ledger_extract import (
    DocumentLedgerExtract,
    PartyField,
)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "eval" / "fixtures" / "direction"


def _load_fixtures() -> list[dict]:
    """Load every direction fixture from the eval/fixtures/direction directory."""
    if not FIXTURES_DIR.is_dir():
        return []
    return [
        json.loads(p.read_text())
        for p in sorted(FIXTURES_DIR.glob("*.json"))
    ]


def _fixture_to_extract(fix: dict) -> DocumentLedgerExtract:
    """Convert a fixture dict into a DocumentLedgerExtract for the resolver."""
    from_party = PartyField(
        name=fix["from_party"]["name"],
        uen=fix["from_party"].get("uen"),
        role="issuer",
    )
    to_party = PartyField(
        name=fix["to_party"]["name"],
        uen=fix["to_party"].get("uen"),
        role="recipient",
    )
    return DocumentLedgerExtract(
        vendor_name=from_party.name,
        customer_name=to_party.name,
        document_reference=fix.get("file_label", "FIX-1"),
        document_date="2026-08-01",
        document_total=100.0,
        from_party=from_party,
        to_party=to_party,
        direction_for_client=fix["expected_direction_for_client"],
    )


def test_batch_direction_fixtures_resolve_to_expected_direction():
    """All six batch fixtures must resolve to the expected direction."""
    fixtures = _load_fixtures()
    assert len(fixtures) == 6, (
        f"expected 6 direction fixtures, got {len(fixtures)}"
    )

    for fix in fixtures:
        extract = _fixture_to_extract(fix)
        resolved = _resolve_direction_from_extract(
            extract.model_dump(),
            fallback="purchase",
        )
        assert resolved == fix["expected_direction_for_client"], (
            f"fixture {fix['file_label']}: expected "
            f"{fix['expected_direction_for_client']!r}, got {resolved!r}"
        )

    unambiguous = sum(
        1 for f in fixtures
        if f["expected_direction_for_client"] in ("purchase", "sales")
    )
    assert unambiguous == 5


def test_batch_direction_fixtures_parties_round_trip():
    """The fixture From/To parties survive the DocumentLedgerExtract round-trip."""
    fixtures = _load_fixtures()
    assert fixtures
    for fix in fixtures:
        extract = _fixture_to_extract(fix)
        assert extract.from_party.role == "issuer"
        assert extract.to_party.role == "recipient"
        if fix["from_party"].get("uen") is not None:
            assert extract.from_party.uen == fix["from_party"]["uen"]
        if fix["to_party"].get("uen") is not None:
            assert extract.to_party.uen == fix["to_party"]["uen"]


def test_batch_direction_fixtures_include_contractor_case():
    """The contractor-invoice fixture is in the regression set."""
    fixtures = _load_fixtures()
    labels = [f.get("file_label", "") for f in fixtures]
    assert any("CON-001" in lbl for lbl in labels), (
        f"contractor invoice fixture missing; labels: {labels}"
    )
