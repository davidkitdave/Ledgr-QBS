"""COA categorization — resolve each invoice line to the client's own account code.

Resolution order (deterministic first, multi-tenant by construction):
  1. Entity_Memory — vendor name/reg-no match -> remembered account + tax (conf 0.95)
  2. Category -> code — a universal category mapped to a client account code (conf 0.9)
  3. COA keyword match — an account's "AI Search Keywords" appears in the line/vendor (conf 0.8)
  4. unresolved -> flagged

``resolve_account`` is pure and deterministic (no LLM) so it is unit-testable.
``categorize_invoice`` batches every still-unresolved line into ONE Gemini structured-output
call against the client's own COA, then fills ``InvoiceLine.account_code``. No account numbers
are hardcoded — everything comes from the client's Client Setup (passed in / read from state).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from .client_context import (
    CoaAccount,
    EntityMemoryEntry,
    category_mapping_from_state,
    coa_from_state,
    entity_memory_from_state,
)
from .models import NormalizedInvoice, PartyInfo


@dataclass
class AccountResolution:
    account_code: Optional[str]
    account_name: Optional[str]
    confidence: float
    source: str
    flagged: bool
    tax_code: Optional[str] = None


def _norm(s: Optional[str]) -> str:
    """Lowercase alphanumerics only — mirrors document_classifier._norm."""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def canonical_party_name(
    party: PartyInfo,
    entity_memory: list[EntityMemoryEntry],
) -> Optional[str]:
    """Return the canonical ``EntityMemoryEntry.name`` when a confident match is found.

    Match criteria (exact only — no fuzzy/substring):
      1. reg_no exact match (both non-empty after _norm).
      2. Exact normalized-name equality.

    Returns None when:
      - No entry matches.
      - The matched entry has an empty name.
      - The matched name already equals party.name (no-op).
    """
    n_party = _norm(party.name)
    n_reg = _norm(getattr(party, "gst_regno", None))

    for entry in entity_memory:
        n_entry_name = _norm(entry.name)
        if not n_entry_name:
            continue

        reg_hit = bool(n_reg) and bool(_norm(entry.reg_no)) and _norm(entry.reg_no) == n_reg
        name_hit = bool(n_party) and n_entry_name == n_party

        if reg_hit or name_hit:
            canon = entry.name
            if not canon:
                return None
            if canon == party.name:
                return None  # already canonical
            return canon

    return None


def _split_keywords(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for chunk in raw.replace(";", ",").replace("/", ",").split(","):
        kw = _norm(chunk)
        if kw:
            out.append(kw)
    return out


def resolve_account(
    line_description: Optional[str],
    vendor_name: Optional[str],
    *,
    coa: list[CoaAccount],
    category_mapping: dict[str, Optional[str]],
    entity_memory: list[EntityMemoryEntry],
    category: Optional[str] = None,
    reg_no: Optional[str] = None,
) -> AccountResolution:
    """Deterministic-first resolution of a single line to a COA account. Pure function."""
    n_vendor = _norm(vendor_name)
    n_reg = _norm(reg_no)
    haystack = _norm(f"{line_description or ''} {vendor_name or ''}")

    # 1) Entity_Memory (deterministic) -------------------------------------- #
    for e in entity_memory:
        if not e.mapping_code:
            continue
        n_name = _norm(e.name)
        name_hit = bool(n_name) and len(n_name) > 3 and n_vendor and (n_name in n_vendor or n_vendor in n_name)
        reg_hit = bool(n_reg) and bool(_norm(e.reg_no)) and _norm(e.reg_no) == n_reg
        if name_hit or reg_hit:
            return AccountResolution(
                account_code=e.mapping_code,
                account_name=e.mapping_code,
                confidence=0.95,
                source="entity_memory",
                flagged=False,
                tax_code=e.tax_code,
            )

    # 2) Category -> client code ------------------------------------------- #
    if category and category in category_mapping:
        code = category_mapping.get(category)
        if code:
            return AccountResolution(
                account_code=code,
                account_name=code,
                confidence=0.9,
                source="category_mapping",
                flagged=False,
            )

    # 3) COA keyword match -------------------------------------------------- #
    for acc in coa:
        for kw in _split_keywords(acc.keywords):
            if kw and kw in haystack:
                return AccountResolution(
                    account_code=acc.code,
                    account_name=acc.description,
                    confidence=0.8,
                    source="coa_keyword",
                    flagged=False,
                )

    # 4) Unresolved --------------------------------------------------------- #
    return AccountResolution(None, None, 0.0, "unresolved", flagged=True)


# --------------------------------------------------------------------------- #
# LLM COA match (one batched structured-output call for all unresolved lines)
# --------------------------------------------------------------------------- #
def _llm_match_lines(
    unresolved: list[tuple[int, str, str]],   # (line_index, description, vendor)
    coa: list[CoaAccount],
    model: Optional[str],
    *,
    tax_registered: Optional[bool] = None,
    client_region: str = "",
    client_currency: str = "",
) -> dict[int, dict]:
    """Return {line_index: {account_key, reason, confidence}} from one Gemini call.

    Returns {} on any failure so categorization never crashes.

    Region context: when ``client_region`` / ``client_currency`` are supplied
    the prompt includes them so the LLM can pick country-appropriate expense
    accounts (e.g. "Service Tax" vs "GST" hint words). Pure Passthrough via
    ADK state templating (``{client_region?}``) — the graph node is expected
    to fill these from session state before calling.
    """
    from google.genai import types

    from ..shared_libraries.genai_client import lite_model, make_client

    coa_for_prompt = [
        {"key": a.key, "description": a.description, "account_type": a.account_type or ""}
        for a in coa
        if a.key
    ]
    valid_keys = {a["key"] for a in coa_for_prompt}
    lines_for_prompt = [
        {"index": idx, "description": desc, "vendor": vendor}
        for (idx, desc, vendor) in unresolved
    ]

    response_schema = {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "account_key": {"type": "string", "nullable": True},
                        "reason": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["index", "account_key", "confidence"],
                },
            }
        },
        "required": ["results"],
    }

    if tax_registered is True:
        gst_ctx = "yes"
    elif tax_registered is False:
        gst_ctx = "no"
    else:
        gst_ctx = "unknown"

    region_ctx = (
        f"\nClient region: {client_region or 'unknown'}\n"
        f"Client base currency: {client_currency or 'unknown'}\n"
        if client_region or client_currency
        else ""
    )

    prompt = (
        "You are an accounting assistant categorizing invoice/receipt lines to a client's "
        "Chart of Accounts (COA). For each line, pick the single best-matching COA account by "
        "its exact `key`, or null if no account is a reasonable fit. Prefer Profit & Loss expense "
        "accounts for purchase costs. Return a short reason and a confidence in [0,1].\n\n"
        "IMPORTANT — your task is ONLY to assign an account_code from the COA list below. "
        "Do NOT choose or infer a tax treatment or GST code (SR/ZR/ES/OS etc.); that is "
        "decided separately by a deterministic master gate and is outside your scope."
        f"{region_ctx}\n"
        f"Client GST-registered: {gst_ctx}\n\n"
        "You MUST return the JSON results object for every line provided. "
        "Never reply empty or omit the results array.\n\n"
        "COA (choose key from these only):\n"
        f"{json.dumps(coa_for_prompt, ensure_ascii=False)}\n\n"
        "Lines to categorize:\n"
        f"{json.dumps(lines_for_prompt, ensure_ascii=False)}\n"
    )

    try:
        client = make_client()
        resp = client.models.generate_content(
            model=model or lite_model(),
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=response_schema,
                temperature=0,
            ),
        )
        data = json.loads(resp.text or "{}")
    except Exception:
        return {}

    out: dict[int, dict] = {}
    for item in data.get("results", []) or []:
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        key = item.get("account_key")
        if key is not None and key not in valid_keys:
            key = None  # hallucinated key -> treat as no match
        out[idx] = {
            "account_key": key,
            "reason": item.get("reason", ""),
            "confidence": float(item.get("confidence") or 0.0),
        }
    return out


def categorize_invoice(
    inv: NormalizedInvoice,
    *,
    coa: list[CoaAccount],
    category_mapping: dict[str, Optional[str]],
    entity_memory: list[EntityMemoryEntry],
    use_llm: bool = True,
    model: Optional[str] = None,
    tax_registered: Optional[bool] = None,
    client_region: str = "",
    client_currency: str = "",
) -> NormalizedInvoice:
    """Fill ``InvoiceLine.account_code`` for every line. Never crashes; returns ``inv``.

    Region context (client_region / client_currency) is forwarded into the LLM
    COA match prompt so it can pick country-appropriate expense accounts.
    Both default to empty string — backward-compatible with every existing
    caller.
    """
    party = inv.counterparty
    vendor_name = party.name
    reg_no = party.gst_regno

    # Contact-master name normalization (WS4.5): replace extracted name with the
    # canonical form from entity_memory so the ERP sees a single consistent ContactName.
    canon = canonical_party_name(party, entity_memory)
    if canon:
        party.name = canon
        vendor_name = canon

    # COA lookup by key -> description, for mapping LLM keys back to names.
    by_key = {a.key: a for a in coa if a.key}

    resolutions: list[AccountResolution] = []
    unresolved: list[tuple[int, str, str]] = []
    for i, line in enumerate(inv.lines):
        res = resolve_account(
            line.description,
            vendor_name,
            coa=coa,
            category_mapping=category_mapping,
            entity_memory=entity_memory,
            reg_no=reg_no,
        )
        resolutions.append(res)
        if res.source == "unresolved":
            unresolved.append((i, line.description or "", vendor_name or ""))

    if unresolved and use_llm and coa:
        matches = _llm_match_lines(
            unresolved,
            coa,
            model,
            tax_registered=tax_registered,
            client_region=client_region,
            client_currency=client_currency,
        )
        for idx, m in matches.items():
            if idx < 0 or idx >= len(resolutions):
                continue
            key = m.get("account_key")
            conf = m.get("confidence", 0.0)
            if key:
                acc = by_key.get(key)
                resolutions[idx] = AccountResolution(
                    account_code=acc.code if acc else key,
                    account_name=acc.description if acc else key,
                    confidence=conf,
                    source="llm_coa",
                    flagged=conf < 0.6,
                )
            else:
                resolutions[idx] = AccountResolution(
                    account_code=None,
                    account_name=None,
                    confidence=conf,
                    source="llm_coa",
                    flagged=True,
                )

    for line, res in zip(inv.lines, resolutions):
        line.account_code = res.account_code or res.account_name or ""

    return inv


# --------------------------------------------------------------------------- #
# ADK FunctionTool wrapper
# --------------------------------------------------------------------------- #
def resolve_account_tool(tool_context, line_description: str, vendor_name: str) -> dict:
    """ADK FunctionTool: resolve a single line to a COA account from ``tool_context.state``.

    Duck-typed: ``tool_context`` only needs a ``.state`` mapping. Returns a dict with a
    ``status`` key ('resolved' / 'unresolved') plus the resolution fields.
    """
    state = getattr(tool_context, "state", {}) or {}
    res = resolve_account(
        line_description,
        vendor_name,
        coa=coa_from_state(state),
        category_mapping=category_mapping_from_state(state),
        entity_memory=entity_memory_from_state(state),
    )
    return {
        "status": "unresolved" if res.source == "unresolved" else "resolved",
        "account_code": res.account_code,
        "account_name": res.account_name,
        "confidence": res.confidence,
        "source": res.source,
        "flagged": res.flagged,
        "tax_code": res.tax_code,
    }
