"""Multi-country jurisdiction routing for the ADK document lane.

The YAU LEE Malaysia session (c92951d1) proved that the previous build was
implicitly Singapore-only: ``tax_node`` always loaded ``sg_gst.yaml`` regardless
of the client profile's ``region``. This module is the single source of truth
for **which tax system / rate band applies** based on session state, and is
designed to be consumed by:

1. The ``resolve_jurisdiction`` @node — runs once per document, writes
   ``state["tax_jurisdiction"]`` for ADK web visibility.
2. The LLM tax reasoning agent — receives the resolved jurisdiction as
   ``{client_region?}`` / ``{tax_jurisdiction?}`` state-template variables in
   its instruction (see ADK docs on state templating).
3. Python rate guards — validates the LLM's per-line math against the
   jurisdiction's reference rate bands.

Per ADK best practices (Sessions/State, Function tools, Dynamic workflows docs):
* Read region/currency from ``state`` (``tool_context.state.get('client_region')``
  or ``ctx.state['region']``) — never hardcode.
* Use ``{key?}`` state templating in LLM instructions — the framework
  auto-injects the right values, no manual string concat.
* Keep Python guards thin (math, tolerance, HITL flag). LLM is the brain.

New jurisdictions are added by:
1. Adding a row to ``REGION_REGISTRY`` (currency, yaml, tax_system,
   cross_border_flag_policy).
2. Dropping a per-jurisdiction reference YAML under
   ``invoice_processing/shared_libraries/{yaml_name}`` (rates + code_map).
3. Optionally adding jurisdiction-specific signal lexicons in the YAML.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml

from invoice_processing.export.tax_classifier import _bands_active_on, _is_standard_scope

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# State keys (kept short; surfaced in ADK web State tab for visibility)
# --------------------------------------------------------------------------- #
TAX_JURISDICTION_KEY = "tax_jurisdiction"
SUPPLIER_COUNTRY_KEY = "supplier_country"
CUSTOMER_COUNTRY_KEY = "customer_country"
TAX_SYSTEM_HINT_KEY = "tax_system_hint"
JURISDICTION_RATES_KEY = "jurisdiction_rates"
FLAG_FOR_HUMAN_KEY = "flag_for_human"
CROSS_BORDER_KEY = "cross_border"

# --------------------------------------------------------------------------- #
# Canonical region codes (mirror ClientContext.region values)
# --------------------------------------------------------------------------- #
REGION_SINGAPORE = "SINGAPORE"
REGION_MALAYSIA = "MALAYSIA"

# Cross-border: client in one country, counterparty in another.
JURISDICTION_CROSS_BORDER = "CROSS_BORDER"
JURISDICTION_AMBIGUOUS = "AMBIGUOUS"

# Tax system labels (for state visibility + LLM prompts).
TAX_SYSTEM_GST = "GST"
TAX_SYSTEM_SST = "SST"
TAX_SYSTEM_OUT_OF_SCOPE = "OS"

# Per-region registry — single source for currency, YAML, tax system, cross-border policy.
# Adding a jurisdiction = drop a YAML + one row here (WS3 onboarding reads supported_regions()).
REGION_REGISTRY: dict[str, dict[str, str]] = {
    REGION_SINGAPORE: {
        "currency": "SGD",
        "yaml": "sg_gst.yaml",
        "tax_system": TAX_SYSTEM_GST,
        "home_country": "SG",
        "cross_border_flag_policy": "partial_exempt",
    },
    REGION_MALAYSIA: {
        "currency": "MYR",
        "yaml": "my_sst.yaml",
        "tax_system": TAX_SYSTEM_SST,
        "home_country": "MY",
        "cross_border_flag_policy": "never",
    },
}


def supported_regions() -> list[str]:
    """Canonical region codes with a registry row (for onboarding dropdowns)."""
    return list(REGION_REGISTRY.keys())


def registration_threshold_for_region(region: str) -> tuple[float, str, str]:
    """Return (amount, currency, label) from the region's reference YAML."""
    entry = REGION_REGISTRY.get(region)
    if not entry:
        return 0.0, "", ""
    data = _load_reference(entry["yaml"])
    reg = data.get("registration_threshold") or {}
    amount = float(reg.get("amount") or 0)
    currency = str(reg.get("currency") or entry["currency"]).strip().upper()
    label = str(reg.get("label") or f"{region} tax registration").strip()
    return amount, currency, label


def _resolve_client_currency(state: dict, client_region: str) -> Optional[str]:
    """Derive client currency from state or registry — never silently default to SGD."""
    raw = state.get("base_currency")
    if raw:
        return str(raw).strip().upper()
    if client_region and client_region in REGION_REGISTRY:
        return REGION_REGISTRY[client_region]["currency"]
    return None


def _cross_border_flag_for_human(
    policy: str,
    *,
    partial_exempt: bool,
) -> bool:
    if policy == "partial_exempt":
        return partial_exempt
    if policy == "never":
        return False
    return True


@dataclass
class JurisdictionRule:
    """A per-region rule set for tax reasoning + validation.

    Attributes:
        code: short jurisdiction key written to ``state["tax_jurisdiction"]``
            ("SINGAPORE", "MALAYSIA", "CROSS_BORDER", "AMBIGUOUS").
        region: the client's home region (mirrors ``state["region"]``).
        tax_system: "GST" | "SST" | "OS" — written to
            ``state["tax_system_hint"]`` for LLM consumption.
        reference_yaml: file name under ``invoice_processing/shared_libraries/``
            used for rate bands + code_map. None for cross-border/ambiguous.
        standard_rate: the current standard rate as a decimal (0.09 = 9%).
            Used by Python rate guards. None if jurisdiction is ambiguous.
        rate_tolerance: absolute fractional tolerance for rate-match checks
            (default 0.01 = 1%). Loaded from YAML when available.
        rate_band_label: human-readable label for the rate band ("9% GST",
            "8% SST"). Surfaced in trace + state tab.
        cross_border: True when client region != counterparty country.
        flag_for_human: True when the system cannot make a confident decision
            and must escalate to HITL.
        notes: free-form notes (jurisdiction selection rationale).
    """

    code: str
    region: str
    tax_system: str
    reference_yaml: Optional[str] = None
    standard_rate: Optional[float] = None
    rate_tolerance: float = 0.01
    rate_band_label: Optional[str] = None
    cross_border: bool = False
    flag_for_human: bool = False
    notes: Optional[str] = None


@dataclass
class JurisdictionResolution:
    """Output of :func:`resolve_jurisdiction`. Pure data — safe to put in state."""

    jurisdiction: JurisdictionRule
    client_region: str
    client_currency: Optional[str]
    supplier_country: Optional[str]
    customer_country: Optional[str]

    def to_state_dict(self) -> dict[str, Any]:
        """Plain-dict view (basic types only) for ``session.state``."""
        return {
            TAX_JURISDICTION_KEY: self.jurisdiction.code,
            TAX_SYSTEM_HINT_KEY: self.jurisdiction.tax_system,
            JURISDICTION_RATES_KEY: {
                "rate_band_label": self.jurisdiction.rate_band_label,
                "standard_rate": self.jurisdiction.standard_rate,
                "rate_tolerance": self.jurisdiction.rate_tolerance,
                "reference_yaml": self.jurisdiction.reference_yaml,
            },
            SUPPLIER_COUNTRY_KEY: self.supplier_country,
            CUSTOMER_COUNTRY_KEY: self.customer_country,
            FLAG_FOR_HUMAN_KEY: self.jurisdiction.flag_for_human,
            CROSS_BORDER_KEY: self.jurisdiction.cross_border,
        }


# --------------------------------------------------------------------------- #
# YAML reference loader (cached per-process)
# --------------------------------------------------------------------------- #
_YAML_CACHE: dict[str, dict] = {}


def _load_reference(yaml_name: str) -> dict:
    """Load a jurisdiction reference YAML (rates + code_map) with caching.

    Path: ``invoice_processing/shared_libraries/{yaml_name}``.
    """
    if yaml_name in _YAML_CACHE:
        return _YAML_CACHE[yaml_name]
    here = Path(__file__).resolve().parent
    path = here.parent / "invoice_processing" / "shared_libraries" / yaml_name
    if not path.is_file():
        logger.warning("jurisdiction reference YAML not found: %s", path)
        _YAML_CACHE[yaml_name] = {}
        return {}
    with open(path, encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}
    _YAML_CACHE[yaml_name] = loaded
    return loaded


def clear_jurisdiction_cache() -> None:
    """Reset cached YAML loads. Test helper."""
    _YAML_CACHE.clear()


def _current_standard_rate(
    yaml_name: Optional[str],
    on: Optional[date] = None,
) -> tuple[Optional[float], Optional[str]]:
    """Pick the standard (non-carve-out) rate from ``rate_by_date`` bands."""
    if not yaml_name:
        return None, None
    data = _load_reference(yaml_name)
    bands = data.get("rate_by_date") or []
    if not bands:
        return None, None
    ref_date = on or date.today()
    active = _bands_active_on(bands, ref_date)
    if not active:
        active = bands
    standard = [b for b in active if _is_standard_scope(b.get("scope"))]
    pool = standard if standard else active
    rate = max(float(b["rate"]) for b in pool)
    label = f"{int(round(rate * 100))}% {data.get('tax_system', '')}".strip()
    return rate, label


# --------------------------------------------------------------------------- #
# Region normalization
# --------------------------------------------------------------------------- #
_REGION_ALIASES = {
    "SG": REGION_SINGAPORE,
    "SGP": REGION_SINGAPORE,
    "SINGAPORE": REGION_SINGAPORE,
    "MY": REGION_MALAYSIA,
    "MYS": REGION_MALAYSIA,
    "MALAYSIA": REGION_MALAYSIA,
    "M'SIA": REGION_MALAYSIA,
    "MSIA": REGION_MALAYSIA,
}


def _norm_region(value: Any) -> str:
    """Normalize a free-form region string to the canonical code.

    Falls back to SINGAPORE only when env ``LEDGR_DEFAULT_REGION`` explicitly
    sets it (otherwise leaves empty so the caller can flag as ambiguous).
    """
    if not value:
        default = os.environ.get("LEDGR_DEFAULT_REGION", "").strip().upper()
        if default in _REGION_ALIASES.values():
            return default
        return ""
    s = str(value).strip().upper()
    return _REGION_ALIASES.get(s, s)


_COUNTRY_ALIASES = {
    "SG": "SG", "SGP": "SG", "SINGAPORE": "SG",
    "MY": "MY", "MYS": "MY", "MALAYSIA": "MY", "M'SIA": "MY", "MSIA": "MY",
}


def _norm_country(value: Any) -> Optional[str]:
    """Normalize a country string to a 2-letter ISO-style code (SG/MY/...)."""
    if not value:
        return None
    s = str(value).strip().upper()
    if not s:
        return None
    return _COUNTRY_ALIASES.get(s, s[:2] if len(s) >= 2 else s)


# --------------------------------------------------------------------------- #
# resolve_jurisdiction — the single source of truth
# --------------------------------------------------------------------------- #
def resolve_jurisdiction(state: dict) -> JurisdictionResolution:
    """Resolve the tax jurisdiction from session state. Pure function.

    Reads:
      * ``state["region"]`` or ``state["client_region"]`` (home region)
      * ``state["base_currency"]``
      * ``state["supplier_country"]`` (set by extract node, may be None)
      * ``state["customer_country"]`` (set by extract node, may be None)

    Returns a :class:`JurisdictionResolution` carrying a :class:`JurisdictionRule`
    that downstream nodes (tax_node, categorize_node, LLM prompts, Python guards)
    can read without re-deriving from raw state.

    Behavior:

    * When client region + currency clearly map to one of the supported
      jurisdictions and the counterparty is in the SAME country → that
      jurisdiction, NOT cross-border.
    * When client region + counterparty country differ → CROSS_BORDER with
      per-region cross_border_flag_policy (SG: partial_exempt only; MY: auto-book).
    * When client region is unknown or unsupported → AMBIGUOUS, flag for human.
    """
    raw_region = state.get("client_region") or state.get("region") or ""
    client_region = _norm_region(raw_region)
    client_currency = _resolve_client_currency(state, client_region)

    supplier_country = _norm_country(state.get(SUPPLIER_COUNTRY_KEY))
    customer_country = _norm_country(state.get(CUSTOMER_COUNTRY_KEY))

    partial_exempt = bool(state.get("partial_exempt"))
    counterparty_country = supplier_country or customer_country

    if not client_region:
        return JurisdictionResolution(
            jurisdiction=JurisdictionRule(
                code=JURISDICTION_AMBIGUOUS,
                region="",
                tax_system="",
                flag_for_human=True,
                notes="client region not set in state; cannot pick a rule set",
            ),
            client_region="",
            client_currency=client_currency,
            supplier_country=supplier_country,
            customer_country=customer_country,
        )

    entry = REGION_REGISTRY.get(client_region)
    if not entry:
        return JurisdictionResolution(
            jurisdiction=JurisdictionRule(
                code=JURISDICTION_AMBIGUOUS,
                region=client_region,
                tax_system="",
                flag_for_human=True,
                notes=f"region={client_region} is not in REGION_REGISTRY; cannot pick a rule set",
            ),
            client_region=client_region,
            client_currency=client_currency,
            supplier_country=supplier_country,
            customer_country=customer_country,
        )

    home_country = entry["home_country"]
    reference_yaml = entry["yaml"]

    if counterparty_country and counterparty_country != home_country:
        flag = _cross_border_flag_for_human(
            entry["cross_border_flag_policy"],
            partial_exempt=partial_exempt,
        )
        tax_label = entry["tax_system"]
        if flag:
            notes = (
                f"Partially-exempt {client_region} client + imported service; "
                "reverse-charge (RC) treatment needs review."
            )
        else:
            notes = (
                f"Foreign counterparty (country={counterparty_country}); "
                f"out of scope for local {tax_label}"
                " — foreign tax recorded as shown, not claimable as input tax."
            )
        return JurisdictionResolution(
            jurisdiction=JurisdictionRule(
                code=JURISDICTION_CROSS_BORDER,
                region=client_region,
                tax_system=TAX_SYSTEM_OUT_OF_SCOPE,
                reference_yaml=reference_yaml,
                standard_rate=None,
                flag_for_human=flag,
                cross_border=True,
                notes=notes,
            ),
            client_region=client_region,
            client_currency=client_currency,
            supplier_country=supplier_country,
            customer_country=customer_country,
        )

    expected_currency = entry["currency"]
    if client_currency != expected_currency:
        return JurisdictionResolution(
            jurisdiction=JurisdictionRule(
                code=JURISDICTION_AMBIGUOUS,
                region=client_region,
                tax_system="",
                flag_for_human=True,
                notes=(
                    f"region={client_region} but base_currency={client_currency}; "
                    "cannot pick a rule set without confirmation"
                ),
            ),
            client_region=client_region,
            client_currency=client_currency,
            supplier_country=supplier_country,
            customer_country=customer_country,
        )

    rate, label = _current_standard_rate(reference_yaml)
    return JurisdictionResolution(
        jurisdiction=JurisdictionRule(
            code=client_region,
            region=client_region,
            tax_system=entry["tax_system"],
            reference_yaml=reference_yaml,
            standard_rate=rate,
            rate_band_label=label,
        ),
        client_region=client_region,
        client_currency=client_currency,
        supplier_country=supplier_country,
        customer_country=customer_country,
    )


def write_to_state(state: dict, resolution: JurisdictionResolution) -> None:
    """Persist the resolution into session state (basic types only).

    Writes the standard set of keys consumed by ``tax_node``, the LLM tax
    agent (via ``{key?}`` state templating), and the ADK web State tab.
    """
    for k, v in resolution.to_state_dict().items():
        # Avoid forcing None into state — leaves cleaner State-tab rendering.
        if v is not None:
            state[k] = v


def resolution_from_state(state: dict) -> JurisdictionResolution:
    """Reconstruct a :class:`JurisdictionResolution` from already-written state keys.

    This is a READ-ONLY helper — it does NOT re-run cross-border or
    party-country determination logic.  It is the companion to
    :func:`write_to_state`: read what ``resolve_jurisdiction_node`` wrote,
    rebuild the typed object so downstream code (tax_node, _reason_one_invoice)
    receives a ``JurisdictionResolution`` without re-resolving.

    Raises ``RuntimeError`` when the mandatory ``tax_jurisdiction`` key is
    absent, enforcing the single-authority invariant loudly rather than
    silently falling back to re-resolution.
    """
    jurisdiction_code = state.get(TAX_JURISDICTION_KEY)
    if not jurisdiction_code:
        raise RuntimeError(
            "resolution_from_state: tax_jurisdiction missing from state; "
            "resolve_jurisdiction_node must run before tax_node"
        )

    tax_system = state.get(TAX_SYSTEM_HINT_KEY, "")
    rates: dict = state.get(JURISDICTION_RATES_KEY) or {}

    # Read persisted flags; fall back to code-derived values for backward compat
    # (old state written before FLAG_FOR_HUMAN_KEY / CROSS_BORDER_KEY were added).
    cross_border = (
        bool(state[CROSS_BORDER_KEY]) if CROSS_BORDER_KEY in state
        else (jurisdiction_code == JURISDICTION_CROSS_BORDER)
    )
    flag_for_human = (
        bool(state[FLAG_FOR_HUMAN_KEY]) if FLAG_FOR_HUMAN_KEY in state
        else (jurisdiction_code in (JURISDICTION_CROSS_BORDER, JURISDICTION_AMBIGUOUS))
    )

    rule = JurisdictionRule(
        code=jurisdiction_code,
        region=state.get("client_region") or state.get("region") or "",
        tax_system=tax_system,
        reference_yaml=rates.get("reference_yaml"),
        standard_rate=rates.get("standard_rate"),
        rate_tolerance=float(rates.get("rate_tolerance") or 0.01),
        rate_band_label=rates.get("rate_band_label"),
        cross_border=cross_border,
        flag_for_human=flag_for_human,
    )

    client_region = _norm_region(
        state.get("client_region") or state.get("region") or ""
    )
    client_currency = _resolve_client_currency(state, client_region)

    return JurisdictionResolution(
        jurisdiction=rule,
        client_region=client_region,
        client_currency=client_currency,
        supplier_country=state.get(SUPPLIER_COUNTRY_KEY),
        customer_country=state.get(CUSTOMER_COUNTRY_KEY),
    )