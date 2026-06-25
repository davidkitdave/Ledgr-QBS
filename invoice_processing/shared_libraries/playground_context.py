"""Shared playground-context helpers.

These functions were previously private to ``accounting_agents.agent`` but are
needed by both the ``accounting_agents`` graph and the lean ``ledgr_agent``.
Moving them here breaks the import dependency of ``ledgr_agent`` on the doomed
``accounting_agents`` graph modules.

Public API:

- :func:`playground_default_context` — build the synthetic
  :class:`~invoice_processing.export.client_context.ClientContext` used when
  no real Slack channel profile is loaded (dev / playground / agents-cli eval).
- :func:`seed_playground_profile_if_needed` — inject that context into an ADK
  state dict when all guards pass (not prod, no existing profile).
"""
from __future__ import annotations


def playground_default_context():
    """Build the synthetic ClientContext used when no real profile loads.

    In dev / playground mode, the ``load_client_profile`` callback injects a
    default client profile so the document lane can run end-to-end without a
    real Slack channel.  To make the playground useful for testing real
    invoices, the defaults can be overridden by env vars (preferred for quick
    tweaks) or by a local ``playground_profile.json`` file dropped in the
    workspace root (preferred for richer profiles).  All values are optional —
    missing ones fall back to the hard-coded defaults.

    Environment variables (all optional)::

        LEDGR_PLAYGROUND_CLIENT_ID      default: "playground"
        LEDGR_PLAYGROUND_CLIENT_NAME    default: "Playground Client"
        LEDGR_PLAYGROUND_CLIENT_UEN     default: ""
        LEDGR_PLAYGROUND_REGION         default: "SINGAPORE"
        LEDGR_PLAYGROUND_SOFTWARE       default: "qbs"
        LEDGR_PLAYGROUND_CURRENCY       default: "SGD"
        LEDGR_PLAYGROUND_TAX_REGISTERED default: "true"
        LEDGR_PLAYGROUND_FYE_MONTH      default: 12

    Or a JSON file at ``playground_profile.json`` (resolved relative to the
    current working directory) with the same keys.
    """
    import json as _json
    import logging as _logging
    import os as _os
    from pathlib import Path as _Path

    from invoice_processing.export.client_context import CoaAccount, EntityMemoryEntry, ClientContext

    defaults: dict = {
        "client_id": "playground",
        "client_name": "Playground Client",
        "client_uen": "",
        "region": "SINGAPORE",
        "software": "qbs",
        "base_currency": "SGD",
        "tax_registered": True,
        "partial_exempt": False,
        "fye_month": 12,
    }

    # Env-var overrides (string -> typed coercion).
    env_map = {
        "client_id": ("LEDGR_PLAYGROUND_CLIENT_ID", str),
        "client_name": ("LEDGR_PLAYGROUND_CLIENT_NAME", str),
        "client_uen": ("LEDGR_PLAYGROUND_CLIENT_UEN", str),
        "region": ("LEDGR_PLAYGROUND_REGION", str),
        "software": ("LEDGR_PLAYGROUND_SOFTWARE", str),
        "base_currency": ("LEDGR_PLAYGROUND_CURRENCY", str),
        "fye_month": ("LEDGR_PLAYGROUND_FYE_MONTH", int),
    }
    for key, (var, caster) in env_map.items():
        raw = _os.environ.get(var)
        if raw is None or raw == "":
            continue
        try:
            defaults[key] = caster(raw)
        except (TypeError, ValueError):
            _logging.getLogger(__name__).warning(
                "Ignoring invalid %s=%r (expected %s)", var, raw, caster.__name__,
            )

    tax_raw = _os.environ.get("LEDGR_PLAYGROUND_TAX_REGISTERED")
    if tax_raw is not None and tax_raw != "":
        defaults["tax_registered"] = tax_raw.strip().lower() in ("true", "1", "yes", "y")

    partial_exempt_raw = _os.environ.get("LEDGR_PLAYGROUND_PARTIAL_EXEMPT")
    if partial_exempt_raw is not None and partial_exempt_raw != "":
        defaults["partial_exempt"] = partial_exempt_raw.strip().lower() in ("true", "1", "yes", "y")

    # JSON-file override (higher precedence than env vars).
    config_path = _Path(_os.environ.get("LEDGR_PLAYGROUND_PROFILE_PATH", "playground_profile.json"))
    coa_rows: list = []
    category_mapping: dict = {}
    entity_memory: list = []
    if config_path.is_file():
        try:
            loaded = _json.loads(config_path.read_text())
            if isinstance(loaded, dict):
                for key in defaults:
                    if key in loaded:
                        defaults[key] = loaded[key]
                # Phase 8 / playground-coa-seed: also seed COA, category
                # mapping, and entity_memory from the JSON profile when
                # present, so the categorize LLM has real accounts to match
                # against (empty coa[] previously caused account_code="" in
                # a past multi-country ADK session).
                coa_rows = list(loaded.get("coa") or [])
                category_mapping = dict(loaded.get("category_mapping") or {})
                entity_memory = list(loaded.get("entity_memory") or [])
                _logging.getLogger(__name__).info(
                    "playground seed: loaded %d profile keys from %s",
                    len(loaded), config_path,
                )
        except (OSError, ValueError) as exc:
            _logging.getLogger(__name__).warning(
                "Failed to read playground profile from %s: %s", config_path, exc,
            )

    # Build CoaAccount / EntityMemoryEntry objects the categorizer can read
    # out of state via ``coa_from_state`` / ``entity_memory_from_state`` —
    # they expect dataclass instances, not raw dicts.
    coa_objects = [
        CoaAccount(
            code=row.get("code"),
            description=row.get("description") or row.get("key") or "",
            account_type=row.get("account_type"),
            financial_statement=row.get("financial_statement"),
            nature=row.get("nature"),
            keywords=row.get("keywords"),
        )
        for row in coa_rows
        if isinstance(row, dict)
    ]
    entity_memory_objects = [
        EntityMemoryEntry(
            name=row.get("name") or "",
            reg_no=row.get("reg_no"),
            mapping_code=row.get("mapping_code"),
            role=row.get("role"),
            tax_code=row.get("tax_code"),
        )
        for row in entity_memory
        if isinstance(row, dict) and row.get("name")
    ]

    return ClientContext(
        client_id=defaults["client_id"],
        client_name=defaults["client_name"],
        client_uen=defaults["client_uen"] or None,
        region=defaults["region"],
        accounting_software=defaults["software"],
        base_currency=defaults["base_currency"],
        tax_registered=bool(defaults["tax_registered"]),
        partial_exempt=bool(defaults["partial_exempt"]),
        fye_month=defaults["fye_month"],
        coa=coa_objects,
        category_mapping=category_mapping,
        entity_memory=entity_memory_objects,
    )


def seed_playground_profile_if_needed(state: dict) -> bool:
    """Inject a synthetic playground ClientContext into *state* when all guards pass.

    Guards (ALL must hold to seed):
    1. ``state`` is not None and is a dict-like mapping.
    2. No existing profile in state (no ``client_id`` and no ``client_name``).
    3. ``config.is_playground_seed_enabled()`` is True (i.e. not prod).

    Returns True if the seed was applied, False otherwise (so callers can log).
    This helper is callable from any module without circular-import risk —
    it operates on a plain state dict and all heavy imports are done lazily
    inside the function body.
    """
    if state is None:
        return False
    if state.get("client_id") is not None or state.get("client_name") is not None:
        return False

    from accounting_agents import config  # lazy — avoids circular import at module level
    if not config.is_playground_seed_enabled():
        return False

    import logging as _logging
    default_ctx = playground_default_context()
    _logging.getLogger(__name__).info(
        "playground seed: no client profile found; injecting ClientContext "
        "(client_id=%s, client_name=%s, software=%s)",
        default_ctx.client_id, default_ctx.client_name, default_ctx.accounting_software,
    )
    for k, v in default_ctx.to_state().items():
        state[k] = v

    # Seed ledger data from local store
    from accounting_agents.local_ledger_store import LocalLedgerStore
    local_store = LocalLedgerStore()
    client_id = state["client_id"]
    latest_fy = local_store.latest_fy(client_id)
    if latest_fy:
        rows = local_store.read_rows(client_id, latest_fy)
        state["ledger_data"] = rows
        state["ledger_row_count"] = len(rows)
        state["fy_loaded"] = latest_fy
        state["fy_pointers"] = local_store.fy_pointers(client_id)
    else:
        state["ledger_data"] = []
        state["ledger_row_count"] = 0
        state["fy_loaded"] = "none"
        state["fy_pointers"] = []

    state["processing_log"] = []
    state["pending_reviews"] = []
    return True
