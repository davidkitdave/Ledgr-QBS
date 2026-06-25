"""Schema-shape assertions for Stream A — tightened read-layer schemas.

These tests lock the structural contract of ExtractedInvoice and
ClassificationResult so that regressions in field required-ness or
enum bounds are caught immediately without needing an LLM call.
"""

from __future__ import annotations

import pytest

from invoice_processing.classify.document_classifier import (
    ALLOWED_DOC_TYPES,
    ClassificationResult,
)
from invoice_processing.extract.invoice_extractor import ExtractedInvoice


# =========================================================================== #
# A.2 — ExtractedInvoice schema assertions
# =========================================================================== #


class TestExtractedInvoiceSchema:
    """ExtractedInvoice.model_json_schema() must have exactly the right required set
    and bounded enum for issuer_tax_system."""

    def _schema(self):
        return ExtractedInvoice.model_json_schema()

    def test_gst_total_is_required(self):
        assert "gst_total" in self._schema()["required"]

    def test_subtotal_is_required(self):
        assert "subtotal" in self._schema()["required"]

    def test_total_is_required(self):
        assert "total" in self._schema()["required"]

    def test_issuer_tax_system_is_required(self):
        assert "issuer_tax_system" in self._schema()["required"]

    def test_issuer_country_is_NOT_required(self):
        """issuer_country is unbounded — must remain Optional, not required."""
        assert "issuer_country" not in self._schema().get("required", [])

    def test_bill_to_country_is_NOT_required(self):
        """bill_to_country is unbounded — must remain Optional, not required."""
        assert "bill_to_country" not in self._schema().get("required", [])

    def test_issuer_tax_system_bounded_enum(self):
        """issuer_tax_system must carry the exact bounded Literal enum."""
        prop = self._schema()["properties"]["issuer_tax_system"]
        # Pydantic renders Literal as {"enum": [...]} in the JSON schema
        enum_values = prop.get("enum") or prop.get("const")
        assert set(enum_values) == {"GST", "SST", "VAT", "NONE"}, (
            f"Expected bounded enum {{GST,SST,VAT,NONE}}, got {enum_values}"
        )

    def test_valid_construction_with_all_required_fields(self):
        """ExtractedInvoice must be constructable with only the required fields."""
        ex = ExtractedInvoice(
            doc_type="invoice",
            issuer_tax_system="SST",
            subtotal=100.0,
            gst_total=8.0,
            total=108.0,
        )
        assert ex.issuer_tax_system == "SST"
        assert ex.subtotal == 100.0
        assert ex.gst_total == 8.0
        assert ex.total == 108.0

    def test_invalid_tax_system_rejected(self):
        """An out-of-set issuer_tax_system value must be rejected at construction."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ExtractedInvoice(
                doc_type="invoice",
                issuer_tax_system="UNKNOWN",
                subtotal=0.0,
                gst_total=0.0,
                total=0.0,
            )

    def test_missing_gst_total_rejected(self):
        """Omitting gst_total must be rejected."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ExtractedInvoice(
                doc_type="invoice",
                issuer_tax_system="NONE",
                subtotal=0.0,
                total=0.0,
            )

    def test_none_tax_system_accepted(self):
        """NONE is a valid issuer_tax_system value (no tax shown on document)."""
        ex = ExtractedInvoice(
            doc_type="invoice",
            issuer_tax_system="NONE",
            subtotal=50.0,
            gst_total=0.0,
            total=50.0,
        )
        assert ex.issuer_tax_system == "NONE"


# =========================================================================== #
# A.1 — ClassificationResult doc_type enum assertion
# =========================================================================== #


class TestClassificationResultSchema:
    """ClassificationResult.doc_type must carry a server-enforced enum matching
    ALLOWED_DOC_TYPES so Gemini's response_schema enforces it server-side."""

    def _schema(self):
        return ClassificationResult.model_json_schema()

    def test_doc_type_has_bounded_enum(self):
        """doc_type must carry enum values exactly matching ALLOWED_DOC_TYPES."""
        prop = self._schema()["properties"]["doc_type"]
        enum_values = prop.get("enum") or prop.get("const")
        assert enum_values is not None, "doc_type must have an enum in the JSON schema"
        assert set(enum_values) == set(ALLOWED_DOC_TYPES), (
            f"doc_type enum {set(enum_values)} != ALLOWED_DOC_TYPES {set(ALLOWED_DOC_TYPES)}"
        )

    def test_doc_type_is_required(self):
        assert "doc_type" in self._schema()["required"]

    def test_valid_doc_type_accepted(self):
        for dt in ALLOWED_DOC_TYPES:
            r = ClassificationResult(doc_type=dt, confidence=0.9, reason="test")
            assert r.doc_type == dt

    def test_invalid_doc_type_rejected(self):
        """An out-of-set doc_type value must be rejected by Pydantic."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ClassificationResult(
                doc_type="purchase_order",
                confidence=0.9,
                reason="test",
            )

    def test_clamp_reassignment_to_other_still_works(self):
        """The post-LLM clamp (result.doc_type = 'other') must not raise."""
        r = ClassificationResult(doc_type="invoice", confidence=0.9, reason="test")
        # This simulates the post-LLM clamp in classify_document()
        r.doc_type = "other"
        assert r.doc_type == "other"
