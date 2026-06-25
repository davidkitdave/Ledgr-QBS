"""Singapore GST per-line tax-code classifier (rules-first).

Implements the ordered decision tables from docs/research/sg-gst-tax-codes.md
(§6.1 purchases, §6.2 sales). Deterministic rules resolve the common cases
(telco/freight ZR split, normal local SR, exempt, overseas/no-GST); anything the
rules cannot resolve is returned as the legal default (SR) but flagged for review.

The taxonomy + target-system code strings live in shared_libraries/sg_gst.yaml so
that Xero / QBS code strings stay outside the classification logic.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml

from .models import InvoiceLine, NormalizedInvoice

_SHARED_LIBS = Path(__file__).resolve().parent.parent / "shared_libraries"
_DEFAULT_YAML = "sg_gst.yaml"
_ALIASES_YAML = "tax_aliases.yaml"

# Per-name cache so repeated TaxClassifier construction in a single process
# (e.g. batch fan-out of 20 docs) doesn't re-read the same YAML from disk.
_TAXONOMY_CACHE: dict[str, dict] = {}

# Cached, parsed keyword-alias ladder (jurisdiction-independent). Loaded once
# from tax_aliases.yaml and reused across all classifier instances.
_KEYWORD_ALIASES_CACHE: Optional[list[tuple[str, str, str]]] = None


def _load_keyword_aliases() -> list[tuple[str, str, str]]:
    """Load+cache the ordered keyword alias ladder as (treatment, match, pattern).

    Mirrors the previously-hardcoded ladder in ``_classify_purchase`` /
    ``_classify_sales``; first match wins, so YAML order is preserved.
    """
    global _KEYWORD_ALIASES_CACHE
    if _KEYWORD_ALIASES_CACHE is None:
        with open(_SHARED_LIBS / _ALIASES_YAML, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        rows = data.get("keyword_aliases") or []
        _KEYWORD_ALIASES_CACHE = [
            (str(r["treatment"]), str(r["match"]), str(r["pattern"]).lower())
            for r in rows
        ]
    return _KEYWORD_ALIASES_CACHE

logger = logging.getLogger(__name__)

# Jurisdiction-string → yaml filename mapping (case-insensitive).
_JURISDICTION_TO_YAML: dict[str, str] = {
    "malaysia": "my_sst.yaml",
    "MY": "my_sst.yaml",
}
_JURISDICTION_UPPER_TO_YAML: dict[str, str] = {
    "SINGAPORE": "sg_gst.yaml",
    "MALAYSIA": "my_sst.yaml",
}


def _load_taxonomy(yaml_name: str = _DEFAULT_YAML) -> dict:
    """Load and cache a taxonomy YAML from shared_libraries by filename."""
    if yaml_name not in _TAXONOMY_CACHE:
        yaml_path = _SHARED_LIBS / yaml_name
        with open(yaml_path, encoding="utf-8") as f:
            _TAXONOMY_CACHE[yaml_name] = yaml.safe_load(f)
    return _TAXONOMY_CACHE[yaml_name]


def get_tax_classifier(reference_yaml: Optional[str] = None) -> Optional["TaxClassifier"]:
    """Build a :class:`TaxClassifier` for the given taxonomy reference.

    Accepts:
    - A bare YAML filename  ("my_sst.yaml", "sg_gst.yaml")
    - A jurisdiction string ("MALAYSIA", "SINGAPORE")

    Returns ``None`` when reference is missing, unrecognised, or the YAML file
    is absent — callers must flag for review instead of silently defaulting to SG.
    """
    ref = (reference_yaml or "").strip()
    if not ref:
        return None

    if not ref.endswith(".yaml"):
        mapped = _JURISDICTION_UPPER_TO_YAML.get(ref.upper())
        if mapped is None:
            logger.warning(
                "get_tax_classifier: unrecognised jurisdiction/reference %r",
                ref,
            )
            return None
        ref = mapped

    yaml_path = _SHARED_LIBS / ref
    if not yaml_path.exists():
        logger.warning(
            "get_tax_classifier: '%s' not found in shared_libraries",
            ref,
        )
        return None

    return TaxClassifier(taxonomy=_load_taxonomy(ref))


def _band_end(band: dict[str, Any]) -> date:
    raw = band.get("to")
    return date.fromisoformat(raw) if raw else date.max


def _bands_active_on(bands: list[dict[str, Any]], d: date) -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    for band in bands:
        frm = date.fromisoformat(band["from"])
        if frm <= d <= _band_end(band):
            active.append(band)
    return active


def _is_standard_scope(scope: Any) -> bool:
    s = str(scope or "").strip().lower()
    return s in ("standard", "all", "")


def _is_carve_out_scope(scope: Any) -> bool:
    return "carve" in str(scope or "").lower()


class TaxClassifier:
    """Classifies invoice lines to canonical SG GST treatments and maps to a target system."""

    def __init__(self, taxonomy: Optional[dict] = None):
        self.tax = taxonomy if taxonomy is not None else _load_taxonomy()
        self._signals = {k: [s.lower() for s in v] for k, v in self.tax["signals"].items()}
        self._home_country = str(self.tax.get("home_country") or "SG").strip().upper()
        self._threshold = self.tax["review"]["confidence_threshold"]
        self._rate_tol = self.tax["review"]["rate_tolerance"]
        self._rate_keyword_set = self.rate_keyword_strings()

    # -- rate by time-of-supply -------------------------------------------------
    def _party_is_overseas(self, party) -> Optional[bool]:
        return party.is_overseas_for(self._home_country)

    def collect_all_rates(self) -> set[float]:
        """Every numeric rate band declared in the taxonomy YAML."""
        rates: set[float] = set()
        for band in self.tax.get("rate_by_date") or []:
            rates.add(float(band["rate"]))
        for band in self.tax.get("sales_tax") or []:
            rates.add(float(band["rate"]))
        for band in self.tax.get("imported_service_tax") or []:
            rates.add(float(band["rate"]))
        return rates

    def rate_keyword_strings(self) -> frozenset[str]:
        """Percent strings (e.g. ``8%``) built from YAML rate bands — not hardcoded."""
        return frozenset(f"{int(round(r * 100))}%" for r in self.collect_all_rates())

    def sales_tax_rates(self) -> list[float]:
        return [float(b["rate"]) for b in self.tax.get("sales_tax") or []]

    def _service_rates_for_date(self, d: Optional[date]) -> list[float]:
        ref = d or date.today()
        bands = _bands_active_on(self.tax.get("rate_by_date") or [], ref)
        return sorted({float(b["rate"]) for b in bands})

    def _is_dual_service_rate_period(self, d: Optional[date]) -> bool:
        return len(self._service_rates_for_date(d)) > 1

    def _carve_out_rate_for_date(self, d: Optional[date]) -> Optional[float]:
        ref = d or date.today()
        carve = [
            float(b["rate"])
            for b in _bands_active_on(self.tax.get("rate_by_date") or [], ref)
            if _is_carve_out_scope(b.get("scope"))
        ]
        return max(carve) if carve else None

    def standard_rate_for_date(self, d: Optional[date]) -> float:
        """Standard (non-carve-out) service/GST rate on the invoice date."""
        bands = self.tax.get("rate_by_date") or []
        if d is None:
            active = bands
        else:
            active = _bands_active_on(bands, d)
        if not active:
            return float(bands[-1]["rate"])
        standard = [b for b in active if _is_standard_scope(b.get("scope"))]
        pool = standard if standard else active
        return max(float(b["rate"]) for b in pool)

    def rate_for_date(self, d: Optional[date]) -> float:
        """Standard rate applicable on the invoice date (defaults to latest)."""
        return self.standard_rate_for_date(d)

    def imported_rate_for_date(self, d: Optional[date]) -> float:
        bands = self.tax.get("imported_service_tax") or []
        if not bands:
            return self.standard_rate_for_date(d)
        ref = d or date.today()
        active = _bands_active_on(bands, ref)
        if active:
            return float(active[-1]["rate"])
        return float(bands[-1]["rate"])

    def allowed_rates_for_treatment(
        self,
        treatment: str,
        line: InvoiceLine,
        inv: NormalizedInvoice,
    ) -> list[float]:
        """Rates that reconcile for a given treatment on this line."""
        d = inv.invoice_date
        if treatment == "SSR":
            return self.sales_tax_rates()
        if treatment == "IM":
            return [self.imported_rate_for_date(d)]
        if treatment == "SR":
            rates = [self.standard_rate_for_date(d)]
            carve = self._carve_out_rate_for_date(d)
            if carve:
                rates.append(carve)
            return sorted(set(rates))
        return []

    def _rate_match_error(self, line: InvoiceLine, rate: float) -> float:
        """Fractional |gst - net*rate| / max(|net|, 1)."""
        if not line.gst_amount or not line.net_amount:
            return float("inf")
        expected = line.net_amount * rate
        denom = max(abs(line.net_amount), 1.0)
        return abs(line.gst_amount - expected) / denom

    def _best_rate_match(
        self,
        line: InvoiceLine,
        rates: list[float],
    ) -> Optional[tuple[float, float]]:
        """Return (rate, error) for the closest rate within tolerance, else None."""
        best_rate: Optional[float] = None
        best_err = float("inf")
        for rate in rates:
            err = self._rate_match_error(line, rate)
            if err <= self._rate_tol and err < best_err:
                best_rate = rate
                best_err = err
        if best_rate is None:
            return None
        return best_rate, best_err

    def _gst_matches_any_rate(self, line: InvoiceLine, rates: list[float]) -> bool:
        if not rates:
            return True
        return self._best_rate_match(line, rates) is not None

    def _reconcile_tax_line(
        self,
        line: InvoiceLine,
        inv: NormalizedInvoice,
    ) -> Optional[tuple[str, float, str]]:
        """If gst/net matches a known band, return (treatment, rate, reason)."""
        if not line.gst_amount or not line.net_amount:
            return None
        candidates: list[tuple[str, float, float]] = []

        for rate in self.sales_tax_rates():
            match = self._best_rate_match(line, [rate])
            if match:
                candidates.append(("SSR", rate, match[1]))

        carve = self._carve_out_rate_for_date(inv.invoice_date)
        dual = self._is_dual_service_rate_period(inv.invoice_date)
        if carve:
            match = self._best_rate_match(line, [carve])
            if match:
                candidates.append(("SR", carve, match[1]))

        std = self.standard_rate_for_date(inv.invoice_date)
        match = self._best_rate_match(line, [std])
        if match:
            candidates.append(("SR", std, match[1]))

        if not dual:
            for rate in self._service_rates_for_date(inv.invoice_date):
                match = self._best_rate_match(line, [rate])
                if match:
                    candidates.append(("SR", rate, match[1]))

        if not candidates:
            return None
        treatment, rate, err = min(candidates, key=lambda c: c[2])
        if treatment == "SSR":
            return (treatment, rate, f"SSR: tax reconciles to sales tax {rate:.0%}")
        if rate == carve:
            return (treatment, rate, f"SR: tax reconciles to carve-out rate {rate:.0%}")
        return (treatment, rate, f"SR: tax reconciles to standard rate {rate:.0%}")

    @staticmethod
    def _alias_matches(kw: str, match: str, pattern: str) -> bool:
        if match == "exact":
            return kw == pattern
        if match == "prefix":
            return kw.startswith(pattern)
        return pattern in kw  # substring

    def _sr_tax_keyword_match(self, kw: str) -> bool:
        for treatment, match, pattern in _load_keyword_aliases():
            if treatment == "SR" and self._alias_matches(kw, match, pattern):
                return True
        return any(rk in kw for rk in self._rate_keyword_set)

    # Confidence + canonical reason verb per treatment for the keyword ladder.
    _KEYWORD_CONF = {"ZR": 0.97, "ES": 0.95, "OS": 0.95, "NT": 0.95, "SR": 0.95}

    def _classify_tax_keyword(self, line: InvoiceLine, kw: str):
        """Resolve an explicit tax_keyword via the shared alias ladder.

        Returns the ``(treatment, conf, flag, reason)`` tuple matching the old
        hardcoded ladder, or ``None`` when no alias (and no rate keyword) hits.
        """
        gst_positive = bool(line.gst_amount and line.gst_amount > 0)
        for treatment, match, pattern in _load_keyword_aliases():
            if treatment == "SR":
                # SR also matches dynamic rate keywords; defer to the shared
                # predicate so the rate-keyword tail keeps working.
                continue
            if self._alias_matches(kw, match, pattern):
                flag = gst_positive
                reason = f"{treatment}: explicit tax_keyword '{line.tax_keyword}'"
                if flag:
                    reason += f"; but gst_amount={line.gst_amount} > 0 — review"
                return treatment, self._KEYWORD_CONF[treatment], flag, reason
        if self._sr_tax_keyword_match(kw):
            return "SR", self._KEYWORD_CONF["SR"], False, (
                f"SR: explicit tax_keyword '{line.tax_keyword}'"
            )
        return None

    # -- signal matching --------------------------------------------------------
    def _matches(self, text: str, key: str) -> bool:
        t = (text or "").lower()
        return any(sig in t for sig in self._signals.get(key, []))

    def _lexicon_tiebreak(self, desc: str) -> Optional[tuple[str, float, bool, str]]:
        """YAML description keywords — soft hint only when no printed tax signal won."""
        conf = 0.55
        if self._matches(desc, "zero_rated"):
            return "ZR", conf, True, "ZR(?): description keyword hint — no printed tax signal"
        if self._matches(desc, "exempt"):
            return "ES", conf, True, "ES(?): description keyword hint — no printed tax signal"
        if self._matches(desc, "no_tax"):
            return "NT", conf, True, "NT(?): description keyword hint — no printed tax signal"
        if self._matches(desc, "standard_rated"):
            return "SR", conf, True, "SR(?): description keyword hint — no printed tax signal"
        return None

    # -- per-line classification ------------------------------------------------
    def classify_line(self, line: InvoiceLine, inv: NormalizedInvoice) -> InvoiceLine:
        # Overseas supplier with no GST line on the document -> out-of-scope
        # (review for reverse charge) BEFORE the tax_visible short-circuit.
        # An overseas supplier legitimately has no SG GST line; the short-
        # circuit would otherwise silently mark it NT and miss the reverse-
        # charge review. ADR-0015 SG-GST decision table, F6 row.
        if (
            inv.doc_type == "purchase"
            and self._party_is_overseas(inv.supplier)
            and inv.tax_visible_on_document is False
        ):
            line.tax_treatment = "OS"
            line.tax_confidence = 0.55
            line.tax_flagged = True
            line.tax_reason = "OS: overseas supplier, no GST — review Reverse Charge"
            return line

        if inv.tax_visible_on_document is False:
            line.tax_treatment = "NT"
            line.tax_confidence = 0.95
            line.tax_flagged = False
            line.tax_reason = "NT: no tax column on document"
            return line

        if inv.doc_type == "sales":
            code, conf, flag, reason = self._classify_sales(line, inv)
        else:
            code, conf, flag, reason = self._classify_purchase(line, inv)

        # Arithmetic check: positive tax must reconcile to an allowed band.
        if line.gst_amount and line.net_amount and code in ("SR", "SSR", "IM"):
            allowed = self.allowed_rates_for_treatment(code, line, inv)
            if allowed and not self._gst_matches_any_rate(line, allowed):
                flag = True
                actual = line.gst_amount / line.net_amount
                reason = (
                    f"{reason}; tax {line.gst_amount} != net*{allowed} "
                    f"(actual {actual:.2%})"
                )

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
        # Ordered alias ladder lives in shared_libraries/tax_aliases.yaml.
        if line.tax_keyword and line.tax_keyword.strip():
            kw = line.tax_keyword.strip().lower()
            resolved = self._classify_tax_keyword(line, kw)
            if resolved is not None:
                return resolved

        # 1. Positive GST amount — reconcile to a known SST/GST band when possible.
        if gst and gst > 0:
            reconciled = self._reconcile_tax_line(line, inv)
            if reconciled:
                treatment, _rate, reason = reconciled
                return treatment, 0.85, False, reason
            if supplier.gst_registered:
                return "SR", 0.92, False, "SR: GST line + supplier GST-registered"
            # GST shown + amount reconciles to the standard rate for the invoice date -> SR, no flag.
            if line.net_amount:
                rate = self.rate_for_date(inv.invoice_date)
                expected = line.net_amount * rate
                denom = max(abs(line.net_amount), 1.0)
                if abs(gst - expected) / denom <= self._rate_tol:
                    return "SR", 0.85, False, f"SR: GST line reconciles to standard rate {rate}"
            # GST shown but no reg no. visible -> suspicious, flag.
            return "SR", 0.5, True, "SR(?): GST shown but no supplier GST reg no."
        # 2. Overseas supplier, no GST line -> out-of-scope, flag for reverse charge.
        if self._party_is_overseas(supplier) and (not gst or gst == 0):
            return "OS", 0.55, True, "OS: overseas supplier, no GST — review Reverse Charge"
        # 3. YAML lexicon tie-break only — no printed tax_keyword and no GST reconcile.
        tiebreak = self._lexicon_tiebreak(desc)
        if tiebreak:
            return tiebreak
        # 4. Supplier not GST-registered / no GST -> no tax.
        if not supplier.gst_registered and (not gst or gst == 0):
            return "NT", 0.95, False, "NT: supplier not GST-registered / no GST line"
        # 5. Indeterminate -> null treatment, flagged for human review.
        return None, 0.4, True, "Unresolved: indeterminate tax treatment — needs human review"

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
        # Ordered alias ladder lives in shared_libraries/tax_aliases.yaml.
        if line.tax_keyword and line.tax_keyword.strip():
            kw = line.tax_keyword.strip().lower()
            resolved = self._classify_tax_keyword(line, kw)
            if resolved is not None:
                return resolved

        # 1. Overseas customer -> zero-rated (verify §21(3) fit).
        if self._party_is_overseas(customer):
            return "ZR", 0.6, True, "ZR: overseas customer"
        # 2. Positive GST line — printed amount is authoritative.
        gst = line.gst_amount
        if gst and gst > 0:
            reconciled = self._reconcile_tax_line(line, inv)
            if reconciled:
                treatment, _rate, reason = reconciled
                return treatment, 0.85, False, reason
            return (
                "SR",
                0.5,
                True,
                "SR(?): printed GST amount does not reconcile to any allowed rate band",
            )
        # 3. YAML lexicon tie-break only — no printed tax_keyword and no GST amount.
        tiebreak = self._lexicon_tiebreak(desc)
        if tiebreak:
            code, conf, flag, reason = tiebreak
            if code == "NT":
                return "OS", conf, flag, "OS(?): description keyword hint — no printed tax signal"
            return code, conf, flag, reason
        # 4. Indeterminate local sale — flag; do not silently book output tax.
        return (
            "SR",
            0.5,
            True,
            "SR: indeterminate local sale — no printed tax signal; review required",
        )

    # -- target-system code string ---------------------------------------------
    def tax_code(
        self,
        treatment: str,
        doc_type: str,
        system: str,
        *,
        rate: Optional[float] = None,
    ) -> str:
        """Map a canonical treatment to the target system's tax-code string.

        ``code_map`` values may be a flat string or a rate-keyed sub-map
        (AutoCount ``SV-6`` / ``SV-8``).
        """
        if not treatment:
            return ""
        direction = "sales" if doc_type == "sales" else "purchase"
        code_maps = self.tax.get("code_map") or {}
        system_map = code_maps.get(system) or {}
        table = system_map.get(direction) or {}
        entry = table.get(treatment)
        if entry is None:
            return ""
        if isinstance(entry, dict):
            if rate is not None:
                key = f"{rate:.2f}"
                if key in entry:
                    return str(entry[key])
                pct_key = str(int(round(rate * 100)))
                if pct_key in entry:
                    return str(entry[pct_key])
            default = entry.get("default")
            if default:
                return str(default)
            return ""
        return str(entry)


def classify_invoice(inv: NormalizedInvoice, classifier: Optional[TaxClassifier] = None) -> NormalizedInvoice:
    """Classify every line of an invoice in place; returns the same invoice."""
    clf = classifier or TaxClassifier()
    for line in inv.lines:
        clf.classify_line(line, inv)
    return inv
