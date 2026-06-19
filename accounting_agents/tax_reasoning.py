"""LLM-first tax reasoning agent.

Replaces the previous SG-only Python ``TaxClassifier`` as the BRAIN of tax
classification. The LLM receives jurisdiction context (region, tax system,
standard rate band) via state templating and decides the per-line
``tax_treatment`` + ``tax_confidence`` + ``tax_reason``.

Python's only role is:

1. **Rate-guard math**: if a line carries a positive GST amount, verify
   ``|gst - net * expected_rate| / max(net, 1) <= rate_tolerance`` — a
   purely numerical check that catches extraction errors or hallucinated
   treatments without the LLM needing to "do the math" itself.
2. **Fallback path**: when the LLM is unreachable, returns no per-line
   decision, or returns a low-confidence aggregate, fall through to the
   existing deterministic ``TaxClassifier`` so behaviour never regresses
   on infra failures (the existing C6-C8 SG golden cases must keep passing
   until an eval shows the LLM path is strictly better).

This module is intentionally decoupled from ADK graph nodes — it exposes
pure async functions consumed by ``nodes.tax_node``. Keeping it free of
``@node`` / ``ctx`` makes it unit-testable in isolation.

ADK best practice alignment (from /adk-docs-mcp):
* LLM prompt uses ``{client_region?}`` / ``{tax_jurisdiction?}`` / etc.
  state templating (auto-injected by ADK at call time).
* Reads region from session state, never hardcoded.
* Outputs a structured pydantic schema so the LLM cannot return free-form
  text into the tax pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, Field

from invoice_processing.export.models import InvoiceLine, NormalizedInvoice

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Structured output schema for the LLM tax agent
# --------------------------------------------------------------------------- #
class LineTaxDecision(BaseModel):
    """One line's tax-treatment decision from the LLM."""

    line_index: int = Field(description="Zero-based index into inv.lines")
    tax_treatment: str = Field(
        description=(
            "Canonical treatment code for this jurisdiction: "
            "SR (Standard-Rated), ZR (Zero-Rated), ES (Exempt), "
            "OS (Out-of-Scope), IM (Imported / Reverse Charge), NT (No-Tax). "
            "For Malaysia SST, also accept SSR (Sales Tax on goods)."
        )
    )
    tax_confidence: float = Field(
        ge=0.0, le=1.0,
        description="Confidence in [0, 1] for this decision.",
    )
    tax_reason: str = Field(
        description="One short sentence — why this treatment was chosen.",
    )
    tax_system: Optional[str] = Field(
        default=None,
        description="Echo of the tax system that applies (GST / SST / OS).",
    )


class TaxDecisionResult(BaseModel):
    """Whole-invoice tax decision from the LLM."""

    decisions: list[LineTaxDecision] = Field(
        default_factory=list,
        description="One entry per line in the invoice.",
    )
    overall_reason: Optional[str] = Field(
        default=None,
        description="Optional summary across all lines.",
    )


# --------------------------------------------------------------------------- #
# LLM prompt construction
# --------------------------------------------------------------------------- #
def _tax_prompt(
    inv: NormalizedInvoice,
    *,
    client_region: str,
    client_currency: str,
    tax_jurisdiction: str,
    tax_system: str,
    rate_band_label: Optional[str],
    standard_rate: Optional[float],
    supplier_country: Optional[str],
    customer_country: Optional[str],
    our_tax_registered: bool,
    reference_yaml: Optional[str],
) -> str:
    """Build the LLM tax prompt. Returns a string the LLM will see.

    The prompt explicitly tells the LLM to:
    * Honour the tax_jurisdiction (it may differ from the client region when
      cross-border; the rule set is authoritative).
    * Apply the local rate band to verify arithmetic.
    * NEVER assume Singapore 9% GST when tax_jurisdiction is MALAYSIA.
    """
    rate_line = f"{rate_band_label} (rate = {standard_rate:.4f})" if standard_rate else "(no standard rate — cross-border / ambiguous)"
    party_lines = []
    if supplier_country:
        party_lines.append(f"- Supplier country: {supplier_country}")
    if customer_country:
        party_lines.append(f"- Customer country: {customer_country}")
    party_block = "\n".join(party_lines) or "- (counterparty country not extracted)"

    lines_block = "\n".join(
        f"  [{i}] desc={ln.description!r} net={ln.net_amount} gst={ln.gst_amount} "
        f"tax_keyword={ln.tax_keyword!r}"
        for i, ln in enumerate(inv.lines)
    )

    return (
        f"You are a per-line tax-reasoning agent for an accounting ledger.\n\n"
        f"# Jurisdiction (authoritative)\n"
        f"- Client region: {client_region}\n"
        f"- Client base currency: {client_currency}\n"
        f"- Tax jurisdiction code: {tax_jurisdiction}\n"
        f"- Tax system: {tax_system}\n"
        f"- Standard rate band: {rate_line}\n"
        f"- Reference table: {reference_yaml or 'none — cross-border / ambiguous'}\n"
        f"- Our client is tax-registered: {our_tax_registered}\n"
        f"{party_block}\n\n"
        f"# Invoice context\n"
        f"- Direction: {inv.doc_type}\n"
        f"- Currency: {inv.currency}\n"
        f"- Invoice date: {inv.invoice_date.isoformat() if inv.invoice_date else 'unknown'}\n"
        f"- tax_visible_on_document: {inv.tax_visible_on_document}\n\n"
        f"# Lines\n"
        f"{lines_block}\n\n"
        f"# Rules\n"
        f"- If our client is NOT tax-registered, every line must be NT (no tax) "
        f"regardless of what the document shows (the tax is part of the cost, "
        f"not a recoverable input).\n"
        f"- If tax_visible_on_document is False, every line is NT.\n"
        f"- If tax_jurisdiction is CROSS_BORDER or AMBIGUOUS, pick OS or NT "
        f"as appropriate and lower confidence (< 0.6) — we cannot make a "
        f"confident decision without human review.\n"
        f"- Otherwise apply the local rate band: lines with positive gst and "
        f"math reconciling to standard_rate (within 1%) → SR; explicit zero-rated "
        f"or 0% wording → ZR; explicit exempt → ES; otherwise NT.\n"
        f"- Do NOT assume Singapore 9% GST when tax_jurisdiction is MALAYSIA. "
        f"The standard rate for MY is 8% SST.\n\n"
        f"# Output (strict JSON)\n"
        f"Return ONE decision per line in the lines block (use the same "
        f"zero-based index). Set tax_confidence in [0, 1]. tax_reason is "
        f"ONE short sentence. overall_reason is optional.\n"
    )


# --------------------------------------------------------------------------- #
# LLM call (single Gemini structured-output request per invoice)
# --------------------------------------------------------------------------- #
def _call_llm(prompt: str, *, model: Optional[str], timeout: float = 30.0) -> Optional[TaxDecisionResult]:
    """Call Gemini with the structured TaxDecisionResult schema.

    Returns None on any failure (network, schema validation, timeout). The
    caller treats None as "fall back to deterministic classifier".
    """
    try:
        from google.genai import types

        from invoice_processing.shared_libraries.genai_client import lite_model, make_client
    except ImportError:
        return None

    try:
        client = make_client()
        resp = client.models.generate_content(
            model=model or lite_model(),
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
                response_schema=TaxDecisionResult,
                max_output_tokens=4096,
            ),
        )
        return TaxDecisionResult.model_validate_json(resp.text or "{}")
    except Exception as exc:  # noqa: BLE001 — never let LLM infra crash tax_node
        logger.warning("tax_reasoning: LLM call failed (%s); will fall back", exc)
        return None


# --------------------------------------------------------------------------- #
# Python rate-guard (the ONLY thing Python does for tax after the LLM)
# --------------------------------------------------------------------------- #
def _validate_rate(
    line: InvoiceLine,
    *,
    expected_rate: Optional[float],
    tolerance: float,
    jurisdiction_code: str,
) -> tuple[Optional[str], bool]:
    """Return (reason_suffix, should_flag) based on the rate-guard math.

    Returns ``reason_suffix`` (appended to ``line.tax_reason``) and
    ``should_flag`` (True only when the math definitively contradicts the
    treatment). On indeterminate input (no gst, no expected rate) this
    returns (None, False) — never flags when there's nothing to check.
    """
    if expected_rate is None:
        return None, False
    if not line.gst_amount or line.gst_amount <= 0:
        return None, False
    if not line.net_amount:
        return None, False
    expected = line.net_amount * expected_rate
    denom = max(abs(line.net_amount), 1.0)
    if abs(line.gst_amount - expected) / denom > tolerance:
        suffix = (
            f"({jurisdiction_code} rate guard: gst {line.gst_amount:.2f} "
            f"vs expected {expected:.2f} at {expected_rate:.0%})"
        )
        return suffix, True
    return None, False


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
@dataclass
class TaxReasoningOutcome:
    """One invoice's final tax outcome after LLM + Python guards."""

    invoice: NormalizedInvoice
    used_llm: bool
    used_fallback: bool
    flagged_count: int
    decisions: list[LineTaxDecision] = field(default_factory=list)


def reason_one_invoice(
    inv: NormalizedInvoice,
    *,
    state: dict,
    jurisdiction_resolution=None,
    model: Optional[str] = None,
) -> TaxReasoningOutcome:
    """Reason over ONE normalized invoice and write the decision back in place.

    Reads jurisdiction from ``state`` (already resolved by the router node)
    or, if not provided, re-resolves via :func:`resolve_jurisdiction`.
    Writes ``tax_treatment`` / ``tax_confidence`` / ``tax_flagged`` /
    ``tax_reason`` on every line.
    """
    from .jurisdiction import (
        resolve_jurisdiction,
        JURISDICTION_RATES_KEY,
    )

    if jurisdiction_resolution is None:
        jurisdiction_resolution = resolve_jurisdiction(state)

    rule = jurisdiction_resolution.jurisdiction

    # Routine cross-border purchase: foreign tax is a cost; book out of scope
    # for the local GST/SST return (not claimable). Deterministic — no LLM, no flag.
    if rule.cross_border and not rule.flag_for_human:
        for line in inv.lines:
            line.tax_treatment = "OS"
            line.tax_confidence = 0.9
            line.tax_flagged = False
            line.tax_reason = (
                rule.notes
                or "Foreign-counterparty purchase; out of scope for local GST/SST; "
                "foreign tax recorded as shown, not claimable as input tax."
            )
        return TaxReasoningOutcome(
            invoice=inv,
            used_llm=False,
            used_fallback=False,
            flagged_count=0,
            decisions=[],
        )

    # Cross-border with flag (e.g. SG partially-exempt) or AMBIGUOUS:
    # forced OS/NT, no LLM, escalate to HITL.
    if rule.flag_for_human:
        for line in inv.lines:
            line.tax_treatment = "OS" if rule.tax_system == "OS" else "NT"
            line.tax_confidence = 0.5
            line.tax_flagged = True
            line.tax_reason = (
                f"{rule.code}: {rule.notes or 'requires review'}; HITL review required"
            )
        return TaxReasoningOutcome(
            invoice=inv,
            used_llm=False,
            used_fallback=False,
            flagged_count=len(inv.lines),
            decisions=[],
        )

    # Happy path: ask the LLM to reason over every line.
    rate_block = state.get(JURISDICTION_RATES_KEY) or {}
    standard_rate = rate_block.get("standard_rate") or rule.standard_rate
    rate_band = rate_block.get("rate_band_label") or rule.rate_band_label
    rate_tol = float(rate_block.get("rate_tolerance") or rule.rate_tolerance or 0.01)

    prompt = _tax_prompt(
        inv,
        client_region=jurisdiction_resolution.client_region,
        client_currency=jurisdiction_resolution.client_currency,
        tax_jurisdiction=rule.code,
        tax_system=rule.tax_system,
        rate_band_label=rate_band,
        standard_rate=standard_rate,
        supplier_country=jurisdiction_resolution.supplier_country,
        customer_country=jurisdiction_resolution.customer_country,
        our_tax_registered=bool(inv.our_gst_registered),
        reference_yaml=rule.reference_yaml,
    )

    decision = _call_llm(prompt, model=model)
    if decision is None or not decision.decisions:
        # LLM unavailable / empty: fall back to deterministic classifier.
        return _fallback_classify(
            inv,
            rule=rule,
            standard_rate=standard_rate,
            rate_tolerance=rate_tol,
        )

    # Apply LLM decisions + Python rate guard.
    flagged = 0
    by_index = {d.line_index: d for d in decision.decisions}
    for i, line in enumerate(inv.lines):
        d = by_index.get(i)
        if d is None:
            # LLM omitted this line — leave undefined and flag.
            line.tax_flagged = True
            line.tax_reason = f"{rule.code}: LLM omitted this line — review"
            flagged += 1
            continue
        line.tax_treatment = d.tax_treatment
        line.tax_confidence = float(d.tax_confidence)
        line.tax_reason = d.tax_reason
        suffix, rate_flag = _validate_rate(
            line,
            expected_rate=standard_rate,
            tolerance=rate_tol,
            jurisdiction_code=rule.code,
        )
        if rate_flag:
            line.tax_flagged = True
            if suffix:
                line.tax_reason = f"{line.tax_reason}; {suffix}" if line.tax_reason else suffix
        else:
            line.tax_flagged = line.tax_confidence < 0.8
        if line.tax_flagged:
            flagged += 1

    return TaxReasoningOutcome(
        invoice=inv,
        used_llm=True,
        used_fallback=False,
        flagged_count=flagged,
        decisions=decision.decisions,
    )


def _fallback_classify(
    inv: NormalizedInvoice,
    *,
    rule,
    standard_rate: Optional[float],
    rate_tolerance: float,
) -> TaxReasoningOutcome:
    """Deterministic fallback when the LLM is unavailable.

    Keeps the previous SG-only ``TaxClassifier`` as the safe path so:
    * Existing SG golden cases keep passing under infra failures.
    * The transition to LLM-first is gradual (run both in dev, compare).
    For Malaysia we apply a similar rules-first pass: SR if math reconciles
    to 8% within tolerance, NT if no tax column, else flag.
    """
    from invoice_processing.export.tax_classifier import TaxClassifier

    flagged = 0
    if rule.region == "SINGAPORE" or rule.code == "SINGAPORE":
        # Use existing SG classifier — preserves C6-C8 golden behaviour.
        clf = TaxClassifier()
        for line in inv.lines:
            clf.classify_line(line, inv)
            if line.tax_flagged:
                flagged += 1
        return TaxReasoningOutcome(
            invoice=inv, used_llm=False, used_fallback=True, flagged_count=flagged
        )

    # Generic Malaysia fallback (very conservative).
    for line in inv.lines:
        if inv.tax_visible_on_document is False:
            line.tax_treatment = "NT"
            line.tax_confidence = 0.95
            line.tax_flagged = False
            line.tax_reason = "NT (fallback): no tax column on document"
            continue
        gst = line.gst_amount or 0.0
        if gst <= 0:
            line.tax_treatment = "NT"
            line.tax_confidence = 0.9
            line.tax_flagged = False
            line.tax_reason = "NT (fallback): no tax amount on line"
            continue
        if standard_rate and line.net_amount:
            expected = line.net_amount * standard_rate
            denom = max(abs(line.net_amount), 1.0)
            if abs(gst - expected) / denom <= rate_tolerance:
                line.tax_treatment = "SR"
                line.tax_confidence = 0.9
                line.tax_flagged = False
                line.tax_reason = (
                    f"SR (fallback): tax reconciles to {standard_rate:.0%} within tolerance"
                )
                continue
        line.tax_treatment = None
        line.tax_confidence = 0.5
        line.tax_flagged = True
        line.tax_reason = "Unresolved: indeterminate — needs human review"
        flagged += 1
    return TaxReasoningOutcome(
        invoice=inv, used_llm=False, used_fallback=True, flagged_count=flagged
    )