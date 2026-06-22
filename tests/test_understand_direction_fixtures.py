"""Hermetic tests for the 6 batch direction fixtures.

These tests exercise the Drive-parity direction logic (the
``_resolve_direction_from_extract`` helper) against captured party
information for six sample batch documents. They run without any API calls
because they feed pre-extracted ``ExtractedDocumentBundle`` objects through the
direction resolver, then assert against the fixture's expected verdict.

The fixtures live in ``eval/fixtures/direction/`` (JSON only, no PDFs).
"""

from __future__ import annotations

import json
from pathlib import Path

from accounting_agents.nodes import _resolve_direction_from_extract
from invoice_processing.extract.ledger_extract import (
    ExtractedDocument,
    ExtractedDocumentBundle,
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


def _fixture_to_bundle(fix: dict) -> ExtractedDocumentBundle:
    """Convert a fixture dict into an ExtractedDocumentBundle for the resolver."""
    doc = ExtractedDocument(
        doc_type="invoice",
        page_range=[1, 1],
        vendor=fix["from_party"]["name"],
        buyer=fix["to_party"]["name"],
        reference=fix.get("file_label", "FIX-1"),
        date="2026-08-01",
        currency="SGD",
        grand_total=100.0,
        vendor_tax_regno=fix["from_party"].get("uen"),
        direction_for_client=fix["expected_direction_for_client"],
    )
    return ExtractedDocumentBundle(documents=[doc])


def test_batch_direction_fixtures_resolve_to_expected_direction():
    """All six batch fixtures must resolve to the expected direction."""
    fixtures = _load_fixtures()
    assert len(fixtures) == 6, (
        f"expected 6 direction fixtures, got {len(fixtures)}"
    )

    for fix in fixtures:
        bundle = _fixture_to_bundle(fix)
        resolved = _resolve_direction_from_extract(
            bundle.model_dump(),
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
    """The fixture From/To parties survive the ExtractedDocument round-trip."""
    fixtures = _load_fixtures()
    assert fixtures
    for fix in fixtures:
        bundle = _fixture_to_bundle(fix)
        doc = bundle.documents[0]
        assert doc.vendor == fix["from_party"]["name"]
        assert doc.buyer == fix["to_party"]["name"]
        if fix["from_party"].get("uen") is not None:
            assert doc.vendor_tax_regno == fix["from_party"]["uen"]


def test_batch_direction_fixtures_include_contractor_case():
    """The contractor-invoice fixture is in the regression set."""
    fixtures = _load_fixtures()
    labels = [f.get("file_label", "") for f in fixtures]
    assert any("CON-001" in lbl for lbl in labels), (
        f"contractor invoice fixture missing; labels: {labels}"
    )
