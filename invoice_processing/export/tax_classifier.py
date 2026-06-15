"""Singapore GST per-line tax-code classifier (rules-first).

Implements the ordered decision tables from docs/research/sg-gst-tax-codes.md
(§6.1 purchases, §6.2 sales). Deterministic rules resolve the common cases
(telco/freight ZR split, normal local SR, exempt, overseas/no-GST); anything the
rules cannot resolve is returned as the legal default (SR) but flagged for review.

The taxonomy + target-system code strings live in shared_libraries/sg_gst.yaml so
that Xero / QBS code strings stay outside the classification logic.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

import yaml

from .models import InvoiceLine, NormalizedInvoice

_GST_YAML = Path(__file__).resolve().parent.parent / "shared_libraries" / "sg_gst.yaml"


def _load_taxonomy() -> dict:
    with open(_GST_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)


class TaxClassifier:
    """Classifies invoice lines to canonical SG GST treatments and maps to a target system."""

    def __init__(self, taxonomy: Optional[dict] = None):
        self.tax = taxonomy or _load_taxonomy()
        self._signals = {k: [s.lower() for s in v] for k, v in self.tax["signals"].items()}
        self._threshold = self.tax["review"]["confidence_threshold"]
        self._rate_tol = self.tax["review"]["rate_tolerance"]

    # -- rate by time-of-supply -------------------------------------------------
    def rate_for_date(self, d: Optional[date]) -> float:
        """Standard GST rate applicable on the invoice date (defaults to latest)."""
        bands = self.tax["rate_by_date"]
        if d is None:
            return bands[-1]["rate"]
        for band in bands:
            frm = date.fromisoformat(band["from"])
            to = date.fromisoformat(band["to"]) if band["to"] else date.max
            if frm <= d <= to:
                return band["rate"]
        return bands[-1]["rate"]

    # -- signal matching --------------------------------------------------------
    def _matches(self, text: str, key: str) -> bool:
        t = (text or "").lower()
        return any(sig in t for sig in self._signals.get(key, []))

    # -- per-line classification ------------------------------------------------
    def classify_line(self, line: InvoiceLine, inv: NormalizedInvoice) -> InvoiceLine:
        if inv.doc_type == "sales":
            code, conf, flag, reason = self._classify_sales(line, inv)
        else:
            code, conf, flag, reason = self._classify_purchase(line, inv)

        # Arithmetic check: if a positive GST is shown, confirm gst ~= net * rate.
        if line.gst_amount and line.net_amount and code in ("SR",):
            rate = self.rate_for_date(inv.invoice_date)
            expected = line.net_amount * rate
            denom = max(abs(line.net_amount), 1.0)
            if abs(line.gst_amount - expected) / denom > self._rate_tol:
                flag = True
                reason = f"{reason}; GST {line.gst_amount} != net*{rate} ({expected:.2f})"

        line.tax_treatment = code
        line.tax_confidence = conf
        line.tax_flagged = flag or conf < self._threshold
        line.tax_reason = reason
        return line

    def _classify_purchase(self, line: InvoiceLine, inv: NormalizedInvoice):
        """§6.1 PURCHASES — first match wins.

        Master gate: if OUR client is NOT GST-registered, every purchase line
        is ``NT`` regardless of what the supplier's invoice says. A
        non-registered Ledgr client cannot reclaim input GST — any GST shown
        on a received invoice becomes part of the cost, not a recoverable
        input. This overrides explicit tax_keyword and signal matches because
        the legal effect on OUR books is the same in either case.
        See memory ``sg-gst-tax-rule-and-xero-codes``.
        """
        if not inv.our_gst_registered:
            return "NT", 0.95, False, (
                "NT: client not GST-registered — input GST treated as cost"
            )

        desc = line.description or ""
        supplier = inv.supplier
        gst = line.gst_amount

        # 0. Explicit tax_keyword from extraction — highest-priority signal.
        if line.tax_keyword and line.tax_keyword.strip():
            kw = line.tax_keyword.strip().lower()
            gst_positive = bool(line.gst_amount and line.gst_amount > 0)
            # ZR: bare "z", starts-with "zr", or contains zero/0% indicators.
            if kw == "z" or kw.startswith("zr") or "zero" in kw or "0%" in kw:
                flag = gst_positive
                reason = f"ZR: explicit tax_keyword '{line.tax_keyword}'"
                if flag:
                    reason += f"; but gst_amount={line.gst_amount} > 0 — review"
                return "ZR", 0.97, flag, reason
            # ES: bare "e", starts-with "es", or contains "exempt".
            if kw == "e" or kw.startswith("es") or "exempt" in kw:
                flag = gst_positive
                reason = f"ES: explicit tax_keyword '{line.tax_keyword}'"
                if flag:
                    reason += f"; but gst_amount={line.gst_amount} > 0 — review"
                return "ES", 0.95, flag, reason
            # OS: starts-with "os" or explicit "out of scope".
            if kw.startswith("os") or "out of scope" in kw:
                flag = gst_positive
                reason = f"OS: explicit tax_keyword '{line.tax_keyword}'"
                if flag:
                    reason += f"; but gst_amount={line.gst_amount} > 0 — review"
                return "OS", 0.95, flag, reason
            # NT: bare "n", starts-with "nt", or contains no-tax indicators.
            if kw == "n" or kw.startswith("nt") or "no tax" in kw or "no-tax" in kw:
                flag = gst_positive
                reason = f"NT: explicit tax_keyword '{line.tax_keyword}'"
                if flag:
                    reason += f"; but gst_amount={line.gst_amount} > 0 — review"
                return "NT", 0.95, flag, reason
            # SR: bare "g" (GST standard-rated), "gst", starts-with "sr"/"tx", or rate%.
            if kw == "g" or kw == "gst" or kw.startswith("sr") or kw.startswith("tx") or "9%" in kw or "8%" in kw or "7%" in kw or "standard" in kw:
                return "SR", 0.95, False, f"SR: explicit tax_keyword '{line.tax_keyword}'"

        # 1. Explicit zero-rated / international service (telco IDD, freight, export).
        if self._matches(desc, "zero_rated"):
            return "ZR", 0.95, False, "ZR: zero-rated/international-service signal"
        # 3 (checked before generic SR): explicit exempt.
        if self._matches(desc, "exempt"):
            return "ES", 0.9, False, "ES: exempt-supply signal"
        # 6 (explicit no-tax wording).
        if self._matches(desc, "no_tax"):
            return "NT", 0.85, False, "NT: no-tax/out-of-scope signal"
        # 2. Positive GST amount + supplier GST-registered -> standard-rated input.
        if gst and gst > 0:
            if supplier.gst_registered:
                return "SR", 0.92, False, "SR: GST line + supplier GST-registered"
            # GST shown + explicit standard-rate wording on the invoice -> SR, no flag.
            # (Covers clean tax invoices like Chubb where reg no. wasn't captured from PDF.)
            if self._matches(desc, "standard_rated"):
                return "SR", 0.88, False, "SR: GST line + explicit standard-rate signal in description"
            # GST shown + amount reconciles to the standard rate for the invoice date -> SR, no flag.
            # The reg no. may simply not have been extracted from the PDF; a correctly-calculated
            # GST amount is strong independent evidence of standard-rated treatment.
            if line.net_amount:
                rate = self.rate_for_date(inv.invoice_date)
                expected = line.net_amount * rate
                denom = max(abs(line.net_amount), 1.0)
                if abs(gst - expected) / denom <= self._rate_tol:
                    return "SR", 0.85, False, f"SR: GST line reconciles to standard rate {rate}"
            # GST shown but no reg no. visible -> suspicious, flag.
            return "SR", 0.5, True, "SR(?): GST shown but no supplier GST reg no."
        # 4. Overseas supplier, no GST line -> out-of-scope, flag for reverse charge.
        if supplier.is_overseas and (not gst or gst == 0):
            return "OS", 0.55, True, "OS: overseas supplier, no GST — review Reverse Charge"
        # 5. Explicit standard-rate wording, no GST amount captured -> SR, no flag.
        # Handles clean tax invoices where the GST amount wasn't extracted separately
        # but the invoice explicitly states a standard-rate treatment (e.g. "GST 9%",
        # "(SR)", "standard-rated"). ZR/ES/NT signals already won above; reaching here
        # means no conflicting signal, so the explicit SR wording is authoritative.
        if self._matches(desc, "standard_rated"):
            return "SR", 0.85, False, "SR: explicit standard-rate signal in description"
        # 6. Supplier not GST-registered / no GST -> no tax.
        if not supplier.gst_registered and (not gst or gst == 0):
            return "NT", 0.7, False, "NT: supplier not GST-registered / no GST line"
        # 7. Indeterminate -> legal default SR, flagged.
        return "SR", 0.4, True, "SR(default): indeterminate — review"

    def _classify_sales(self, line: InvoiceLine, inv: NormalizedInvoice):
        """§6.2 SALES — first match wins.

        Master gate: if OUR client is NOT GST-registered they cannot legally
        charge GST on sales. Every sales line is ``NT`` regardless of what's
        written on the invoice. Hoisted above the tax_keyword block so an
        accidental ``"SR"`` keyword on a non-registered client's invoice does
        not bypass this rule. See memory ``sg-gst-tax-rule-and-xero-codes``.
        """
        if not inv.our_gst_registered:
            return "NT", 0.95, False, "NT: client not GST-registered"

        desc = line.description or ""
        customer = inv.customer

        # 0. Explicit tax_keyword from extraction — highest-priority signal.
        if line.tax_keyword and line.tax_keyword.strip():
            kw = line.tax_keyword.strip().lower()
            gst_positive = bool(line.gst_amount and line.gst_amount > 0)
            # ZR: bare "z", starts-with "zr", or contains zero/0% indicators.
            if kw == "z" or kw.startswith("zr") or "zero" in kw or "0%" in kw:
                flag = gst_positive
                reason = f"ZR: explicit tax_keyword '{line.tax_keyword}'"
                if flag:
                    reason += f"; but gst_amount={line.gst_amount} > 0 — review"
                return "ZR", 0.97, flag, reason
            # ES: bare "e", starts-with "es", or contains "exempt".
            if kw == "e" or kw.startswith("es") or "exempt" in kw:
                flag = gst_positive
                reason = f"ES: explicit tax_keyword '{line.tax_keyword}'"
                if flag:
                    reason += f"; but gst_amount={line.gst_amount} > 0 — review"
                return "ES", 0.95, flag, reason
            # OS: starts-with "os" or explicit "out of scope".
            if kw.startswith("os") or "out of scope" in kw:
                flag = gst_positive
                reason = f"OS: explicit tax_keyword '{line.tax_keyword}'"
                if flag:
                    reason += f"; but gst_amount={line.gst_amount} > 0 — review"
                return "OS", 0.95, flag, reason
            # NT: bare "n", starts-with "nt", or contains no-tax indicators.
            if kw == "n" or kw.startswith("nt") or "no tax" in kw or "no-tax" in kw:
                flag = gst_positive
                reason = f"NT: explicit tax_keyword '{line.tax_keyword}'"
                if flag:
                    reason += f"; but gst_amount={line.gst_amount} > 0 — review"
                return "NT", 0.95, flag, reason
            # SR: bare "g" (GST standard-rated), "gst", starts-with "sr"/"tx", or rate%.
            if kw == "g" or kw == "gst" or kw.startswith("sr") or kw.startswith("tx") or "9%" in kw or "8%" in kw or "7%" in kw or "standard" in kw:
                return "SR", 0.95, False, f"SR: explicit tax_keyword '{line.tax_keyword}'"

        # 2. Export / international service -> zero-rated (verify §21(3) fit).
        if self._matches(desc, "zero_rated") or customer.is_overseas:
            conf = 0.85 if self._matches(desc, "zero_rated") else 0.6
            flag = conf < self._threshold
            return "ZR", conf, flag, "ZR: export/international-service or overseas customer"
        # 3. Exempt supply.
        if self._matches(desc, "exempt"):
            return "ES", 0.9, False, "ES: exempt-supply signal"
        # 4. Explicit out-of-scope.
        if self._matches(desc, "no_tax"):
            return "OS", 0.8, False, "OS: out-of-scope signal"
        # 1. Default local sale -> standard-rated.
        return "SR", 0.9, False, "SR: local standard-rated supply"

    # -- target-system code string ---------------------------------------------
    def tax_code(self, treatment: str, doc_type: str, system: str) -> str:
        """Map a canonical treatment to the target system's tax-code string."""
        direction = "sales" if doc_type == "sales" else "purchase"
        table = self.tax["code_map"][system][direction]
        return table.get(treatment, table.get("SR", ""))


def classify_invoice(inv: NormalizedInvoice, classifier: Optional[TaxClassifier] = None) -> NormalizedInvoice:
    """Classify every line of an invoice in place; returns the same invoice."""
    clf = classifier or TaxClassifier()
    for line in inv.lines:
        clf.classify_line(line, inv)
    return inv
