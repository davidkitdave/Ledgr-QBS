"""Unit tests for resolve_direction — Task 2: direction robustness.

Covers:
1. Fuzzy / normalised name matching (typo tolerance).
2. UEN match preferred over name match when client_uen is supplied.
3. Self-referential / dividend guard: client-as-own-vendor must NOT become purchase.
"""

from __future__ import annotations

from invoice_processing.classify.document_classifier import (
    ClassificationResult,
    resolve_direction,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _cls(
    doc_type: str = "invoice",
    issuer: str | None = None,
    bill_to: str | None = None,
    issuer_uen: str | None = None,
    bill_to_uen: str | None = None,
) -> ClassificationResult:
    return ClassificationResult(
        doc_type=doc_type,
        issuer_name=issuer,
        bill_to_name=bill_to,
        currency="SGD",
        total_amount=100.0,
        confidence=0.95,
        reason="test stub",
    )


# --------------------------------------------------------------------------- #
# 1. Baseline — existing exact behaviour is preserved
# --------------------------------------------------------------------------- #

class TestExactMatch:
    def test_exact_purchase(self):
        result = resolve_direction(
            _cls(issuer="Acme Supplier Pte Ltd", bill_to="Sanesea International"),
            client_name="Sanesea International",
        )
        assert result == "purchase"

    def test_exact_sales(self):
        result = resolve_direction(
            _cls(issuer="Sanesea International", bill_to="Some Customer"),
            client_name="Sanesea International",
        )
        assert result == "sales"

    def test_non_invoice_na(self):
        result = resolve_direction(
            _cls(doc_type="bank_statement", issuer="Any Bank", bill_to="Sanesea International"),
            client_name="Sanesea International",
        )
        assert result == "n/a"

    def test_no_client_name_unknown(self):
        result = resolve_direction(
            _cls(issuer="Acme", bill_to="Sanesea"),
            client_name=None,
        )
        assert result == "unknown"


# --------------------------------------------------------------------------- #
# 2. Fuzzy / normalised name match
# --------------------------------------------------------------------------- #

class TestFuzzyNameMatch:
    """Typos and abbreviations that exact substring matching fails on."""

    def test_typo_in_bill_to_is_purchase(self):
        # "SANERSEA" is a plausible OCR typo for "Sanesea" — must still be purchase.
        result = resolve_direction(
            _cls(issuer="Some Vendor Pte Ltd", bill_to="SANERSEA INTERNATIONAL"),
            client_name="Sanesea International",
        )
        assert result == "purchase", (
            "Fuzzy match should resolve 'SANERSEA' -> 'Sanesea' as purchase"
        )

    def test_typo_in_issuer_is_sales(self):
        # Issuer has a 1-char transposition — still sales.
        result = resolve_direction(
            _cls(issuer="Sanesee International", bill_to="Customer Ltd"),
            client_name="Sanesea International",
        )
        assert result == "sales", (
            "Fuzzy match should resolve 'Sanesee' -> 'Sanesea' as sales"
        )

    def test_abbreviation_purchase(self):
        # "SANESEA INTL" abbreviated form of "Sanesea International".
        result = resolve_direction(
            _cls(issuer="Vendor Corp", bill_to="SANESEA INTL"),
            client_name="Sanesea International",
        )
        # Token overlap: "sanesea" matches -> should be purchase
        assert result == "purchase"

    def test_no_match_on_very_different_name_is_unknown(self):
        result = resolve_direction(
            _cls(issuer="Alpha Corp", bill_to="Beta Ltd"),
            client_name="Sanesea International",
        )
        assert result == "unknown"

    def test_short_name_guard_still_applies(self):
        # bill_to is "XY" (len 2 after norm) — too short, should not match even
        # if ratio is high by accident.
        result = resolve_direction(
            _cls(issuer="Another Vendor", bill_to="XY"),
            client_name="XY",
        )
        # "xy" has norm-len 2, len > 3 guard: no match -> unknown
        assert result == "unknown"


# --------------------------------------------------------------------------- #
# 3. UEN match — preferred over name when client_uen is supplied
# --------------------------------------------------------------------------- #

class TestUenMatch:
    """UEN (company registration number) match is exact and takes priority."""

    def test_uen_match_bill_to_is_purchase(self):
        # UEN in bill_to field of ClassificationResult (via issuer_gst_regno or
        # bill_to_uen on the result, or embedded in bill_to_name).  The
        # ClassificationResult may not carry a separate UEN field for bill-to; we
        # pass it as part of the name for now (the extractor sometimes includes it).
        # The test verifies that when client_uen exactly matches the issuer/bill_to
        # UEN carried on the ClassificationResult, direction is resolved correctly
        # even if the name is completely garbled.
        result = resolve_direction(
            _cls(issuer="Gobbledigook Corp 201234567A", bill_to="XYZXYZ 200099001Z"),
            client_name="Sanesea International",
            client_uen="200099001Z",
        )
        assert result == "purchase", (
            "UEN match on bill_to should resolve to purchase even when name is garbled"
        )

    def test_uen_match_issuer_is_sales(self):
        result = resolve_direction(
            _cls(issuer="Gobbledigook 200099001Z", bill_to="Customer Corp"),
            client_name="Sanesea International",
            client_uen="200099001Z",
        )
        assert result == "sales", (
            "UEN match on issuer should resolve to sales even when name is garbled"
        )

    def test_uen_match_beats_fuzzy_name(self):
        # Both UEN and a fuzzy name would agree -> purchase via UEN path.
        result = resolve_direction(
            _cls(issuer="Some Vendor", bill_to="Sanesea Intl 200099001Z"),
            client_name="Sanesea International",
            client_uen="200099001Z",
        )
        assert result == "purchase"

    def test_uen_not_present_falls_back_to_name(self):
        # client_uen supplied but not present in doc -> fall through to name match.
        result = resolve_direction(
            _cls(issuer="Vendor Pte Ltd", bill_to="Sanesea International"),
            client_name="Sanesea International",
            client_uen="200099001Z",
        )
        assert result == "purchase"

    def test_uen_none_no_uen_match(self):
        # client_uen is None -> no UEN path, normal name match.
        result = resolve_direction(
            _cls(issuer="Vendor", bill_to="Sanesea International"),
            client_name="Sanesea International",
            client_uen=None,
        )
        assert result == "purchase"


# --------------------------------------------------------------------------- #
# 4. Self-referential / dividend guard
# --------------------------------------------------------------------------- #

class TestSelfReferentialGuard:
    """A document whose issuer == client AND bill_to == client must never be
    booked as purchase (client would become its own vendor).  Same for
    dividend / payout keyword documents.
    """

    def test_self_referential_both_sides_is_not_purchase(self):
        # Issuer AND bill-to are both the client — this is self-referential.
        result = resolve_direction(
            _cls(issuer="Sanesea International", bill_to="Sanesea International"),
            client_name="Sanesea International",
        )
        # Must NOT be "purchase"; acceptable outcomes: "self_referential" or "unknown"
        assert result not in ("purchase",), (
            f"Self-referential doc must not be booked as purchase, got {result!r}"
        )

    def test_dividend_keyword_in_reason_not_purchase(self):
        # A doc whose issuer IS the client but carries a dividend/payout flavour.
        # We model this as issuer == client, bill_to == client (typical dividend cert).
        cls = ClassificationResult(
            doc_type="invoice",
            issuer_name="Sanesea International",
            bill_to_name="Sanesea International",
            currency="SGD",
            total_amount=5000.0,
            confidence=0.9,
            reason="Dividend payout certificate issued by and to client",
        )
        result = resolve_direction(cls, client_name="Sanesea International")
        assert result not in ("purchase",), (
            f"Dividend/self-ref doc must not be purchase, got {result!r}"
        )

    def test_normal_purchase_unaffected(self):
        # Regular supplier -> client: still purchase.
        result = resolve_direction(
            _cls(issuer="Unrelated Supplier", bill_to="Sanesea International"),
            client_name="Sanesea International",
        )
        assert result == "purchase"

    def test_normal_sales_unaffected(self):
        # Client -> customer: still sales.
        result = resolve_direction(
            _cls(issuer="Sanesea International", bill_to="Customer Co"),
            client_name="Sanesea International",
        )
        assert result == "sales"

    def test_self_referential_with_uen_not_purchase(self):
        # Even when UEN matches both sides, self-referential must not be purchase.
        result = resolve_direction(
            _cls(
                issuer="Sanesea International 200099001Z",
                bill_to="Sanesea International 200099001Z",
            ),
            client_name="Sanesea International",
            client_uen="200099001Z",
        )
        assert result not in ("purchase",), (
            "Self-referential with UEN must not be purchase"
        )
