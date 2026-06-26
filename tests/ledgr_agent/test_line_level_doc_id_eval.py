"""Issue #28 — line-level deterministic eval via source-doc-id tagging.

Every exported row must carry a STABLE source document id end-to-end
(extraction → export) so the golden scorer can join live ``process_document_batch``
output rows to an expected manifest keyed by doc id and score per-line tax
treatment, COA, and direction deterministically (ADR-0026 §5 — field-match,
NOT LLM-as-judge).

All data here is 100% synthetic. No real client/vendor names, no model calls,
no network. The ZR→ES test is the anti-false-green anchor: a miscoded line that
still reconciles (math intact) must FAIL the line-level eval — proving the
line scorer catches what doc-level/reconcile is blind to.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from invoice_processing.export.exporters import QbsLedgerExporter, XeroLedgerExporter
from invoice_processing.export.models import InvoiceLine, NormalizedInvoice, PartyInfo

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LINE_LEVEL_FIXTURE = (
    _REPO_ROOT / "tests" / "eval" / "datasets" / "golden_line_level_sample.json"
)


# ---------------------------------------------------------------------------
# Criterion 1 — stable source-doc-id origin
# ---------------------------------------------------------------------------


class TestSourceDocId:
    def test_id_is_stable_across_two_identical_builds(self):
        from invoice_processing.export.source_doc_id import source_doc_id_for_invoice

        inv = NormalizedInvoice(
            doc_type="purchase",
            invoice_number="INV-001",
            page_range=(1, 1),
        )
        a = source_doc_id_for_invoice(inv, source_basename="alpha.pdf")
        b = source_doc_id_for_invoice(inv, source_basename="alpha.pdf")
        assert a == b
        assert a  # non-empty

    def test_id_excludes_volatile_slack_file_id(self):
        """The id must not embed the per-run Slack file id (it rotates)."""
        from invoice_processing.export.source_doc_id import source_doc_id_for_invoice

        inv = NormalizedInvoice(invoice_number="INV-001", page_range=(1, 1))
        inv.source_file_id = "F_RUN_A_123"
        a = source_doc_id_for_invoice(inv, source_basename="alpha.pdf")
        inv.source_file_id = "F_RUN_B_999"
        b = source_doc_id_for_invoice(inv, source_basename="alpha.pdf")
        assert a == b

    def test_fanout_distinct_logical_docs_get_distinct_ids(self):
        """Two invoices from one multi-doc PDF (distinct page ranges) → distinct ids."""
        from invoice_processing.export.source_doc_id import source_doc_id_for_invoice

        doc1 = NormalizedInvoice(invoice_number="INV-001", page_range=(1, 1))
        doc2 = NormalizedInvoice(invoice_number="INV-002", page_range=(2, 2))
        id1 = source_doc_id_for_invoice(doc1, source_basename="bundle.pdf")
        id2 = source_doc_id_for_invoice(doc2, source_basename="bundle.pdf")
        assert id1 != id2

    def test_fanout_same_reference_different_pages_still_distinct(self):
        """Page range disambiguates docs even when references collide."""
        from invoice_processing.export.source_doc_id import source_doc_id_for_invoice

        doc1 = NormalizedInvoice(invoice_number="", page_range=(1, 1))
        doc2 = NormalizedInvoice(invoice_number="", page_range=(2, 2))
        id1 = source_doc_id_for_invoice(doc1, source_basename="bundle.pdf", index=0)
        id2 = source_doc_id_for_invoice(doc2, source_basename="bundle.pdf", index=1)
        assert id1 != id2


# ---------------------------------------------------------------------------
# Criterion 1 — rows() stamps source_doc_id without polluting the human Excel
# ---------------------------------------------------------------------------


def _one_line_invoice(*, doc_type="purchase", tax="SR", coa="610-000", sdid="bundle.pdf:INV-001:1-1"):
    inv = NormalizedInvoice(
        doc_type=doc_type,
        invoice_number="INV-001",
        currency="MYR",
        page_range=(1, 1),
        supplier=PartyInfo(name="Vendor Alpha Sdn Bhd"),
        customer=PartyInfo(name="Vendor Alpha Sdn Bhd"),
    )
    inv.source_doc_id = sdid
    inv.lines = [
        InvoiceLine(
            description="Widgets",
            net_amount=1000.0,
            gst_amount=60.0,
            account_code=coa,
            tax_treatment=tax,
        )
    ]
    return inv


class TestRowsCarrySourceDocId:
    def test_qbs_row_has_source_doc_id_key(self):
        inv = _one_line_invoice()
        rows = QbsLedgerExporter().rows([inv], "purchase")
        assert rows
        assert rows[0]["source_doc_id"] == "bundle.pdf:INV-001:1-1"

    def test_qbs_row_carries_line_tax_coa_direction_for_join(self):
        inv = _one_line_invoice(tax="SR", coa="610-000", doc_type="purchase")
        rows = QbsLedgerExporter().rows([inv], "purchase")
        row = rows[0]
        assert row["tax_treatment"] == "SR"
        assert row["account_code"] == "610-000"
        assert row["direction"] == "purchase"

    def test_source_doc_id_not_in_human_columns(self):
        """The provenance keys must never become workbook columns."""
        exporter = QbsLedgerExporter()
        for col in (*exporter.purchase_cols, *exporter.sales_cols):
            assert col not in ("source_doc_id", "tax_treatment", "account_code", "direction")

    def test_xero_row_has_source_doc_id_key(self):
        inv = _one_line_invoice(doc_type="purchase")
        rows = XeroLedgerExporter().rows([inv], "purchase")
        assert rows[0]["source_doc_id"] == "bundle.pdf:INV-001:1-1"


# ---------------------------------------------------------------------------
# Criterion 2 — scorer joins live rows to docs by source_doc_id (non-N/A)
# ---------------------------------------------------------------------------


def _golden_doc(*, file, sdid, tax_code, coa_code, erp_codes, doc_type="invoice"):
    return {
        "file": file,
        "source_doc_id": sdid,
        "doc_type": doc_type,
        "vendor": "Vendor Alpha Sdn Bhd",
        "currency": "MYR",
        "total": 1060.0,
        "tax_amount": 60.0,
        "creditor_code": "400-A0001",
        "lines": [
            {
                "tax_code": tax_code,
                "coa_code": coa_code,
                "erp_codes": erp_codes,
            }
        ],
    }


def _batch_with_tagged_rows(rows: list[dict]) -> dict:
    """Minimal BatchResult dict carrying tagged export_rows."""
    return {
        "status": "success",
        "client_id": "test-client",
        "firm_id": None,
        "documents_processed": len(rows),
        "credits": {"credits_used": 0, "credit_status": "not_billable"},
        "per_file": [],
        "posted_documents": [
            {
                "path": f"/uploads/{r['workbook_file']}",
                "file_name": r["workbook_file"],
                "doc_type": "invoice",
                "invoice_number": r.get("Invoice Number") or r.get("invoice_number"),
                "total": 1060.0,
                "source_doc_id": r["source_doc_id"],
            }
            for r in rows
        ],
        "skipped_documents": [],
        "export_rows": rows,
    }


def _tagged_row(*, file, sdid, tax_treatment, account_code, direction="purchase"):
    return {
        "workbook": "Ledger_FY2025",
        "workbook_file": file,
        "sheet": "Purchase" if direction == "purchase" else "Sales",
        "Invoice Number": "INV-001",
        "Account Code / COA": account_code,
        "source_doc_id": sdid,
        "tax_treatment": tax_treatment,
        "account_code": account_code,
        "direction": direction,
    }


class TestScorerJoinsBySourceDocId:
    def test_per_line_tax_coa_is_not_na_when_rows_carry_source_doc_id(self):
        """The whole point of #28: a row with a source_doc_id must NOT be N/A."""
        from ledgr_agent.metrics.golden_field_match import project_batch

        rows = [
            _tagged_row(
                file="alpha.pdf",
                sdid="alpha.pdf:INV-001:1-1",
                tax_treatment="SR",
                account_code="610-000",
            )
        ]
        batch = _batch_with_tagged_rows(rows)
        projection = project_batch(batch)
        docs = projection["docs_by_file"]["alpha.pdf"]
        # The projected doc must now carry the joined line (NOT lines:[]).
        assert docs[0]["lines"], "expected joined line data, got empty lines (the #28 bug)"
        line = docs[0]["lines"][0]
        assert line["tax_code"] == "SR"
        assert line["coa_code"] == "610-000"

    def test_scorer_tax_coa_scores_one_on_correct_coding(self):
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        rows = [
            _tagged_row(
                file="alpha.pdf",
                sdid="alpha.pdf:INV-001:1-1",
                tax_treatment="SR",
                account_code="610-000",
            )
        ]
        manifest = {
            "file_expectations": {
                "alpha.pdf": {"expected_doc_count": 1, "expected_billable_credits": 1}
            },
            "documents": [
                _golden_doc(
                    file="alpha.pdf",
                    sdid="alpha.pdf:INV-001:1-1",
                    tax_code="SR",
                    coa_code="610-000",
                    erp_codes={"qbs": "SR"},
                )
            ],
        }
        result = _run_scorer(golden_field_match_code, rows, manifest)
        # tax_coa for the matched ERP must be present and scoring 1.0 (not N/A).
        assert "tax_coa[qbs]=1.0" in result["explanation"], result["explanation"]

    def test_tax_coa_and_direction_all_non_na_on_live_shaped_run(self):
        """Criterion 2: per-line tax, COA, AND direction all score non-N/A when
        the live rows carry a source_doc_id joined to a golden by that id.
        """
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        rows = [
            _tagged_row(
                file="alpha.pdf",
                sdid="alpha.pdf:INV-001:1-1",
                tax_treatment="SR",
                account_code="610-000",
                direction="purchase",
            )
        ]
        golden = _golden_doc(
            file="alpha.pdf",
            sdid="alpha.pdf:INV-001:1-1",
            tax_code="SR",
            coa_code="610-000",
            erp_codes={"qbs": "SR"},
        )
        golden["direction"] = "purchase"
        manifest = {
            "file_expectations": {
                "alpha.pdf": {"expected_doc_count": 1, "expected_billable_credits": 1}
            },
            "documents": [golden],
        }
        exp = _run_scorer(golden_field_match_code, rows, manifest)["explanation"]
        assert "tax_coa[qbs]=1.0" in exp, exp
        assert "direction=1.0" in exp, exp
        # And none of the three line dimensions degraded to N/A.
        assert "tax_coa[qbs]=N/A" not in exp
        assert "direction=N/A" not in exp

    def test_wrong_direction_line_fails(self):
        """A line booked on the wrong sheet (sales vs purchase) must fail the
        direction scorer even though tax/COA are correct.
        """
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        rows = [
            _tagged_row(
                file="alpha.pdf",
                sdid="alpha.pdf:INV-001:1-1",
                tax_treatment="SR",
                account_code="610-000",
                direction="sales",  # WRONG — golden expects purchase
            )
        ]
        golden = _golden_doc(
            file="alpha.pdf",
            sdid="alpha.pdf:INV-001:1-1",
            tax_code="SR",
            coa_code="610-000",
            erp_codes={"qbs": "SR"},
        )
        golden["direction"] = "purchase"
        manifest = {
            "file_expectations": {
                "alpha.pdf": {"expected_doc_count": 1, "expected_billable_credits": 1}
            },
            "documents": [golden],
        }
        exp = _run_scorer(golden_field_match_code, rows, manifest)["explanation"]
        assert "direction=0.0" in exp, exp


# ---------------------------------------------------------------------------
# Criterion 3 — the ANTI-FALSE-GREEN anchor: ZR mis-coded as ES
# ---------------------------------------------------------------------------


class TestZrMiscodeFailsButReconciles:
    """A zero-rated line mis-coded exempt keeps the math intact (gst_amount=0,
    totals tie out) so reconcile passes — but the line-level eval MUST fail.
    """

    def _manifest_expecting_zr(self):
        return {
            "file_expectations": {
                "telco.pdf": {"expected_doc_count": 1, "expected_billable_credits": 1}
            },
            "documents": [
                _golden_doc(
                    file="telco.pdf",
                    sdid="telco.pdf:INV-T1:1-1",
                    tax_code="ZR",
                    coa_code="640-000",
                    erp_codes={"qbs": "ZR"},
                )
            ],
        }

    def test_correct_zr_coding_passes_line_scorer(self):
        from ledgr_agent.metrics.golden_field_match import score_tax_coa

        golden = self._manifest_expecting_zr()["documents"][0]
        actual = {
            "doc_type": "invoice",
            "lines": [
                {"tax_code": "ZR", "coa_code": "640-000", "erp_codes": {"qbs": "ZR"}}
            ],
        }
        result = score_tax_coa(golden, actual, "qbs")
        assert result["score"] == 1.0, result["explanation"]

    def test_miscoded_es_fails_line_scorer(self):
        from ledgr_agent.metrics.golden_field_match import score_tax_coa

        golden = self._manifest_expecting_zr()["documents"][0]
        # ES instead of ZR — reconcile is blind to this (gst still 0), but the
        # field scorer must catch it.
        actual = {
            "doc_type": "invoice",
            "lines": [
                {"tax_code": "ES", "coa_code": "640-000", "erp_codes": {"qbs": "ES"}}
            ],
        }
        result = score_tax_coa(golden, actual, "qbs")
        assert result["score"] == 0.0, (
            f"ZR→ES miscode must FAIL the line scorer, got {result['score']}. "
            f"{result['explanation']}"
        )

    def test_end_to_end_miscode_drags_overall_below_correct(self):
        """Full entry point: the miscoded trace must score strictly lower than
        the correctly-coded trace on the SAME golden — the anti-false-green lock.
        """
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        manifest = self._manifest_expecting_zr()
        good_rows = [
            _tagged_row(
                file="telco.pdf",
                sdid="telco.pdf:INV-T1:1-1",
                tax_treatment="ZR",
                account_code="640-000",
            )
        ]
        bad_rows = [
            _tagged_row(
                file="telco.pdf",
                sdid="telco.pdf:INV-T1:1-1",
                tax_treatment="ES",
                account_code="640-000",
            )
        ]
        good = _run_scorer(golden_field_match_code, good_rows, manifest)
        bad = _run_scorer(golden_field_match_code, bad_rows, manifest)
        assert bad["score"] < good["score"], (
            f"miscode score {bad['score']} should be < correct {good['score']}; "
            f"bad={bad['explanation']} good={good['explanation']}"
        )


# ---------------------------------------------------------------------------
# Criterion 4 — committed in-repo synthetic fixture + optional real-golden env
# ---------------------------------------------------------------------------


class TestCommittedInRepoFixture:
    def test_fixture_exists_and_is_pii_free(self):
        import json

        assert _LINE_LEVEL_FIXTURE.exists()
        blob = _LINE_LEVEL_FIXTURE.read_text()
        # Hard PII rule: no real client/vendor/firm tokens that have leaked
        # from sample data in this project's history.
        banned = [
            "JBI", "ATOM AUTO", "YAU LEE", "M PREMIUM", "AKAR", "ROSEBERY",
            "CAST UNITY", "SINGTEL", "STARHUB", "TAT SING",
        ]
        upper = blob.upper()
        hits = [t for t in banned if t in upper]
        assert not hits, f"committed fixture contains banned tokens: {hits}"
        # Every invoice document carries a source_doc_id (the join key).
        data = json.loads(blob)
        for doc in data["documents"]:
            assert doc.get("source_doc_id"), doc

    def test_zr_to_es_miscode_fails_against_committed_fixture(self, monkeypatch):
        """End-to-end against the REAL committed fixture: correct ZR coding
        passes, ZR→ES miscode fails — both reconcile (gst=0). Anti-false-green.
        """
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        monkeypatch.setenv("LEDGR_GOLDEN_MANIFEST", str(_LINE_LEVEL_FIXTURE))
        sdid = "telco-line-level-dec2025.pdf:INV-T1:1-1"
        file = "telco-line-level-dec2025.pdf"

        good = golden_field_match_code(
            _make_instance(
                _batch_with_tagged_rows(
                    [_tagged_row(file=file, sdid=sdid, tax_treatment="ZR", account_code="640-000")]
                )
            )
        )
        bad = golden_field_match_code(
            _make_instance(
                _batch_with_tagged_rows(
                    [_tagged_row(file=file, sdid=sdid, tax_treatment="ES", account_code="640-000")]
                )
            )
        )
        assert "tax_coa[qbs]=1.0" in good["explanation"], good["explanation"]
        assert "tax_coa[qbs]=0.0" in bad["explanation"], bad["explanation"]
        assert bad["score"] < good["score"]


class TestRealGoldenEnvIsOptional:
    def test_default_manifest_used_when_env_unset(self, monkeypatch):
        """When LEDGR_GOLDEN_MANIFEST is unset the scorer falls back to the
        committed default manifest (no crash, no real-data dependency).
        """
        from ledgr_agent.metrics import golden_field_match as gfm

        monkeypatch.delenv("LEDGR_GOLDEN_MANIFEST", raising=False)
        # The scorer resolves the default committed manifest path internally.
        assert gfm._DEFAULT_MANIFEST.exists()
        # A trace with no tool call scores 0.0 deterministically — proving the
        # default path loads without requiring the env var or real data.
        result = gfm.golden_field_match_code(_make_instance({}))
        assert result["score"] == 0.0

    def test_real_golden_path_honoured_when_env_set(self, monkeypatch):
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        monkeypatch.setenv("LEDGR_GOLDEN_MANIFEST", str(_LINE_LEVEL_FIXTURE))
        rows = [
            _tagged_row(
                file="alpha-line-level-dec2025.pdf",
                sdid="alpha-line-level-dec2025.pdf:INV-A1:1-1",
                tax_treatment="SR",
                account_code="610-000",
            )
        ]
        result = golden_field_match_code(
            _make_instance(_batch_with_tagged_rows(rows))
        )
        assert result["score"] is not None
        assert "tax_coa[qbs]=1.0" in result["explanation"]


# ---------------------------------------------------------------------------
# Fix 1 — empty join must FAIL, not score N/A→pass
# ---------------------------------------------------------------------------


class TestEmptyJoinFailsNotNA:
    """When a golden doc authors lines but the export_rows join produces nothing
    (broken tagging), line sub-scores must return 0.0, NOT N/A which would be
    silently dropped from the mean and let the overall float up to ~0.83.

    This is the false-green pattern the project has been bitten by.
    """

    def _manifest_with_authored_lines(self):
        return {
            "file_expectations": {
                "alpha.pdf": {"expected_doc_count": 1, "expected_billable_credits": 1}
            },
            "documents": [
                {
                    "file": "alpha.pdf",
                    "source_doc_id": "alpha.pdf:INV-001:1-1",
                    "doc_type": "invoice",
                    "direction": "purchase",
                    "lines": [
                        {
                            "tax_code": "SR",
                            "coa_code": "610-000",
                            "erp_codes": {"qbs": "SR"},
                        }
                    ],
                }
            ],
        }

    def _batch_no_export_rows(self) -> dict:
        """Live batch with a posted_document but ZERO export_rows (broken tagging)."""
        return {
            "status": "success",
            "client_id": "test-client",
            "firm_id": None,
            "documents_processed": 1,
            "credits": {"credits_used": 1, "credit_status": "charged"},
            "per_file": [],
            "posted_documents": [
                {
                    "path": "/uploads/alpha.pdf",
                    "file_name": "alpha.pdf",
                    "doc_type": "invoice",
                    "invoice_number": "INV-001",
                    "total": 1060.0,
                    "source_doc_id": "alpha.pdf:INV-001:1-1",
                }
            ],
            "skipped_documents": [],
            "export_rows": [],  # <- broken tagging: no rows at all
        }

    def test_score_tax_coa_returns_zero_not_na_when_actual_lines_empty_but_golden_authored(self):
        """Fix 1 core: actual_lines=[] + golden authored lines → score 0.0, not N/A."""
        from ledgr_agent.metrics.golden_field_match import score_tax_coa

        golden_doc = self._manifest_with_authored_lines()["documents"][0]
        actual_doc_no_lines = {"doc_type": "invoice", "lines": []}
        result = score_tax_coa(golden_doc, actual_doc_no_lines, "qbs")
        assert result["score"] == 0.0, (
            f"Expected 0.0 when golden authored lines but actual has none; "
            f"got {result['score']!r}. explanation={result['explanation']!r}"
        )
        assert "0" in result["explanation"] or "join" in result["explanation"].lower(), (
            f"Explanation should mention the join miss: {result['explanation']!r}"
        )

    def test_score_line_direction_returns_zero_not_na_when_actual_lines_empty_but_golden_authored(self):
        """Fix 1 direction: golden has direction authored, actual_lines=[] → 0.0."""
        from ledgr_agent.metrics.golden_field_match import score_line_direction

        golden_doc = {"direction": "purchase", "source_doc_id": "alpha.pdf:INV-001:1-1", "lines": [{"tax_code": "SR"}]}
        actual_doc_no_lines = {"doc_type": "invoice", "lines": []}
        result = score_line_direction(golden_doc, actual_doc_no_lines)
        assert result["score"] == 0.0, (
            f"Expected 0.0 when golden has direction but actual has no lines; "
            f"got {result['score']!r}. explanation={result['explanation']!r}"
        )

    def test_overall_is_failure_not_pass_when_join_produces_nothing(self):
        """Fix 1 end-to-end: broken tagging (export_rows=[]) must produce a
        clearly failing score, NOT ~0.83 pass from N/A sub-scores being dropped.
        """
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        manifest = self._manifest_with_authored_lines()
        batch = self._batch_no_export_rows()

        # Run scorer with broken tagging
        result = _run_scorer(
            golden_field_match_code,
            [],  # no rows — but we need full batch control; use helper directly
            manifest,
        )
        # Actually call with the raw batch (no rows) via the full entry-point path
        fd, path = __import__("tempfile").mkstemp(suffix=".json")
        import os as _os
        with _os.fdopen(fd, "w") as fh:
            __import__("json").dump(manifest, fh)
        old = _os.environ.get("LEDGR_GOLDEN_MANIFEST")
        _os.environ["LEDGR_GOLDEN_MANIFEST"] = path
        try:
            result = golden_field_match_code(_make_instance(batch))
        finally:
            if old is None:
                _os.environ.pop("LEDGR_GOLDEN_MANIFEST", None)
            else:
                _os.environ["LEDGR_GOLDEN_MANIFEST"] = old
            _os.unlink(path)

        # A correct tagging run gets ~1.0; a broken-join run must be clearly below
        # the false-green ~0.83 pattern (line dims were N/A and dropped from mean).
        assert result["score"] <= 0.6, (
            f"Broken-join overall={result['score']:.4f} should be <= 0.6 (false-green). "
            f"explanation={result['explanation']!r}"
        )


# ---------------------------------------------------------------------------
# Fix 2 — no positional fallback when source_doc_id is set but mismatches
# ---------------------------------------------------------------------------


class TestNoPositionalFallbackOnDocIdMismatch:
    """When a golden doc carries source_doc_id but that id matches no live row,
    the scorer must NOT positionally pair it with actual_docs_for_file[i].
    Positional pairing can score golden doc A against live doc B in a reordered
    multi-doc file — reintroducing the fragility #28 set out to remove.

    Legacy manifests (no source_doc_id at all on any doc) keep positional pairing
    for backward compat (the 54 existing golden_v2 tests must stay green).
    """

    def _two_doc_manifest(self):
        """Two docs from one file, both authored with source_doc_ids."""
        return {
            "file_expectations": {
                "bundle.pdf": {"expected_doc_count": 2, "expected_billable_credits": 2}
            },
            "documents": [
                {
                    "file": "bundle.pdf",
                    "source_doc_id": "bundle.pdf:INV-A:1-1",
                    "doc_type": "invoice",
                    "direction": "purchase",
                    "lines": [
                        {
                            "tax_code": "SR",
                            "coa_code": "610-000",
                            "erp_codes": {"qbs": "SR"},
                        }
                    ],
                },
                {
                    "file": "bundle.pdf",
                    "source_doc_id": "bundle.pdf:INV-B:2-2",
                    "doc_type": "invoice",
                    "direction": "purchase",
                    "lines": [
                        {
                            "tax_code": "ZR",
                            "coa_code": "640-000",
                            "erp_codes": {"qbs": "ZR"},
                        }
                    ],
                },
            ],
        }

    def _batch_with_swapped_order(self) -> dict:
        """Live batch where doc B is listed first (fan-out reorder).
        The golden expects INV-A at position 0 and INV-B at position 1, but the
        live rows have them swapped — only id-based join handles this correctly.
        """
        rows = [
            # B first (wrong position)
            _tagged_row(
                file="bundle.pdf",
                sdid="bundle.pdf:INV-B:2-2",
                tax_treatment="ZR",
                account_code="640-000",
                direction="purchase",
            ),
            # A second (wrong position)
            _tagged_row(
                file="bundle.pdf",
                sdid="bundle.pdf:INV-A:1-1",
                tax_treatment="SR",
                account_code="610-000",
                direction="purchase",
            ),
        ]
        return {
            "status": "success",
            "client_id": "test-client",
            "firm_id": None,
            "documents_processed": 2,
            "credits": {"credits_used": 2, "credit_status": "charged"},
            "per_file": [],
            "posted_documents": [
                {
                    "path": "/uploads/bundle.pdf",
                    "file_name": "bundle.pdf",
                    "doc_type": "invoice",
                    "invoice_number": "INV-B",
                    "total": 500.0,
                    "source_doc_id": "bundle.pdf:INV-B:2-2",
                },
                {
                    "path": "/uploads/bundle.pdf",
                    "file_name": "bundle.pdf",
                    "doc_type": "invoice",
                    "invoice_number": "INV-A",
                    "total": 1060.0,
                    "source_doc_id": "bundle.pdf:INV-A:1-1",
                },
            ],
            "skipped_documents": [],
            "export_rows": rows,
        }

    def _batch_with_id_stale_mismatch(self) -> dict:
        """Live batch where a golden source_doc_id is present but no live row
        matches it (e.g. page range changed, ref changed) — must NOT positionally
        pair; must score the line dims 0.0/fail.
        """
        rows = [
            _tagged_row(
                file="bundle.pdf",
                sdid="bundle.pdf:INV-A:1-1",  # only A is present
                tax_treatment="SR",
                account_code="610-000",
                direction="purchase",
            ),
            # INV-B row is ABSENT (stale id — would have matched position 1)
        ]
        return {
            "status": "success",
            "client_id": "test-client",
            "firm_id": None,
            "documents_processed": 2,
            "credits": {"credits_used": 1, "credit_status": "charged"},
            "per_file": [],
            "posted_documents": [
                {
                    "path": "/uploads/bundle.pdf",
                    "file_name": "bundle.pdf",
                    "doc_type": "invoice",
                    "invoice_number": "INV-A",
                    "total": 1060.0,
                    "source_doc_id": "bundle.pdf:INV-A:1-1",
                },
            ],
            "skipped_documents": [],
            "export_rows": rows,
        }

    def test_swapped_order_still_scores_correctly_via_id_join(self):
        """Fix 2 key case: reordered fan-out must score 1.0 on both docs when
        joined by source_doc_id, not 0.0 from positional mis-alignment.
        """
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        manifest = self._two_doc_manifest()
        batch = self._batch_with_swapped_order()

        fd, path = __import__("tempfile").mkstemp(suffix=".json")
        import os as _os
        with _os.fdopen(fd, "w") as fh:
            __import__("json").dump(manifest, fh)
        old = _os.environ.get("LEDGR_GOLDEN_MANIFEST")
        _os.environ["LEDGR_GOLDEN_MANIFEST"] = path
        try:
            result = golden_field_match_code(_make_instance(batch))
        finally:
            if old is None:
                _os.environ.pop("LEDGR_GOLDEN_MANIFEST", None)
            else:
                _os.environ["LEDGR_GOLDEN_MANIFEST"] = old
            _os.unlink(path)

        # Both docs must score 1.0 on their tax_coa — id-join resolves reorder
        exp = result["explanation"]
        assert "tax_coa[qbs]=1.0" in exp, (
            f"Reordered fan-out must still score 1.0 via id-join. "
            f"explanation={exp!r}"
        )
        assert "tax_coa[qbs]=0.0" not in exp, (
            f"No doc should mis-score due to positional pairing. "
            f"explanation={exp!r}"
        )

    def test_stale_doc_id_scores_zero_not_positional_guess(self):
        """Fix 2 id-mismatch: when golden doc B's source_doc_id matches no live
        row, it must score 0.0 (not a positional guess against live doc A's lines).
        """
        from ledgr_agent.metrics.golden_field_match import golden_field_match_code

        manifest = self._two_doc_manifest()
        batch = self._batch_with_id_stale_mismatch()

        fd, path = __import__("tempfile").mkstemp(suffix=".json")
        import os as _os
        with _os.fdopen(fd, "w") as fh:
            __import__("json").dump(manifest, fh)
        old = _os.environ.get("LEDGR_GOLDEN_MANIFEST")
        _os.environ["LEDGR_GOLDEN_MANIFEST"] = path
        try:
            result = golden_field_match_code(_make_instance(batch))
        finally:
            if old is None:
                _os.environ.pop("LEDGR_GOLDEN_MANIFEST", None)
            else:
                _os.environ["LEDGR_GOLDEN_MANIFEST"] = old
            _os.unlink(path)

        # INV-A should score 1.0; INV-B (unmatched) must score 0.0 on line dims
        # not silently positional-pair with INV-A's lines.
        # The overall must therefore be below what it would be if both scored 1.0.
        exp = result["explanation"]
        # We expect at least one 0.0 tax_coa for the unmatched doc B
        assert "tax_coa[qbs]=0.0" in exp, (
            f"Unmatched golden doc must score 0.0 on line dims, not positional guess. "
            f"explanation={exp!r}"
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_instance(batch_payload: dict) -> dict:
    return {
        "agent_data": {
            "turns": [
                {
                    "events": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "function_response": {
                                            "name": "process_document_batch",
                                            "response": batch_payload,
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    }


def _run_scorer(scorer, rows, manifest, *, tmp_path=None):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(manifest, fh)
    old = os.environ.get("LEDGR_GOLDEN_MANIFEST")
    os.environ["LEDGR_GOLDEN_MANIFEST"] = path
    try:
        return scorer(_make_instance(_batch_with_tagged_rows(rows)))
    finally:
        if old is None:
            os.environ.pop("LEDGR_GOLDEN_MANIFEST", None)
        else:
            os.environ["LEDGR_GOLDEN_MANIFEST"] = old
        os.unlink(path)
