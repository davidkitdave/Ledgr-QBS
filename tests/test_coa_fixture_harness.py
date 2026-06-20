"""COA fixture pipeline: parse → validate → ingest (ADR-0006)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.coa_ingest import coa_rows_from_file, ingest_coa
from app.coa_validate import validate_coa
from tests.test_app_coa_ingest import _pending_store

FIXTURES = Path(__file__).resolve().parent.parent / "eval" / "fixtures" / "coa"


class TestCoaFixturePipeline:
    @pytest.mark.parametrize(
        "filename",
        ["valid_qbs_style.csv", "valid_coded.csv"],
    )
    def test_valid_fixtures_pass_validation_and_ingest(self, filename: str):
        path = FIXTURES / filename
        rows = coa_rows_from_file(str(path))
        assert rows

        validation = validate_coa(rows)
        assert validation.ok, validation.errors

        store = _pending_store("C-FIX-1")
        posted: list[dict] = []
        outcome = ingest_coa(
            channel_id="C-FIX-1",
            store=store,
            rows=rows,
            say_fn=lambda **kw: posted.append(kw),
        )
        assert outcome.status == "active"
        assert store.get_by_channel("C-FIX-1").status == "active"
        assert "blocks" in posted[0]

    @pytest.mark.parametrize(
        "filename",
        ["invalid_missing_type.csv", "invalid_duplicate_code.csv"],
    )
    def test_invalid_fixtures_fail_validation(self, filename: str):
        path = FIXTURES / filename
        rows = coa_rows_from_file(str(path))
        assert rows

        validation = validate_coa(rows)
        assert not validation.ok
        assert validation.errors

    def test_invalid_fixture_does_not_activate_client(self):
        path = FIXTURES / "invalid_missing_type.csv"
        rows = coa_rows_from_file(str(path))
        store = _pending_store("C-FIX-2")
        posted: list[dict] = []
        outcome = ingest_coa(
            channel_id="C-FIX-2",
            store=store,
            rows=rows,
            say_fn=lambda **kw: posted.append(kw),
        )
        assert outcome.status == "validation_failed"
        assert store.get_by_channel("C-FIX-2").status == "pending_coa"
        assert "blocks" in posted[0]
