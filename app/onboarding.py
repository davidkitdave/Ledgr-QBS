"""Pure onboarding logic — no Slack API calls, fully unit-testable."""

from __future__ import annotations

from dataclasses import dataclass

from accounting_agents.jurisdiction import REGION_REGISTRY


@dataclass
class ProfileInput:
    client_name: str
    region: str
    fye_month: int
    accounting_software: str
    gst_registered: bool


def parse_modal_state(view: dict) -> ProfileInput:
    """Extract and validate the 4 fields from a Slack view submission state dict.

    Reads ``view["state"]["values"]`` using the block_id / action_id pairs
    defined in ``blocks.onboarding_modal``:
      - client_name / val   → plain_text_input
      - fye_month / val     → static_select
      - accounting_software / val → static_select
      - gst_registered / val → radio_buttons

    Raises:
        ValueError: if any required field is missing or cannot be parsed.
    """
    try:
        values = view["state"]["values"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"view['state']['values'] not accessible: {exc}") from exc

    def _get(block_id: str, action_id: str = "val"):
        block = values.get(block_id)
        if not block:
            raise ValueError(f"Missing block '{block_id}' in modal state")
        element = block.get(action_id)
        if element is None:
            raise ValueError(f"Missing action '{action_id}' in block '{block_id}'")
        return element

    # client_name
    name_el = _get("client_name")
    client_name = (name_el.get("value") or "").strip()
    if not client_name:
        raise ValueError("client_name is required")

    # region
    region_el = _get("region")
    selected_region = region_el.get("selected_option")
    if not selected_region:
        raise ValueError("region is required")
    region = (selected_region.get("value") or "").strip().upper()
    if not region:
        raise ValueError("region value is empty")
    if region not in REGION_REGISTRY:
        raise ValueError(f"region must be one of {list(REGION_REGISTRY.keys())}, got {region!r}")

    # fye_month
    fye_el = _get("fye_month")
    selected_fye = fye_el.get("selected_option")
    if not selected_fye:
        raise ValueError("fye_month is required")
    try:
        fye_month = int(selected_fye["value"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError(f"fye_month value is invalid: {exc}") from exc
    if not 1 <= fye_month <= 12:
        raise ValueError(f"fye_month must be 1-12, got {fye_month}")

    # accounting_software
    sw_el = _get("accounting_software")
    selected_sw = sw_el.get("selected_option")
    if not selected_sw:
        raise ValueError("accounting_software is required")
    accounting_software = (selected_sw.get("value") or "").strip()
    if not accounting_software:
        raise ValueError("accounting_software value is empty")

    # gst_registered
    gst_el = _get("gst_registered")
    selected_gst = gst_el.get("selected_option")
    if not selected_gst:
        raise ValueError("gst_registered is required")
    gst_registered = selected_gst.get("value") == "yes"

    return ProfileInput(
        client_name=client_name,
        region=region,
        fye_month=fye_month,
        accounting_software=accounting_software,
        gst_registered=gst_registered,
    )


def profile_doc(
    inp: ProfileInput,
    *,
    channel_id: str,
    team_id: str,
    client_id: str,
) -> dict:
    """Build the spec §1 Firestore profile dict from parsed modal input.

    Base currency is derived from ``inp.region`` via ``REGION_REGISTRY``.
    Defaults: status="pending_coa", category_mapping={}.
    """
    base_currency = REGION_REGISTRY[inp.region]["currency"]
    return {
        "client_id": client_id,
        "channel_id": channel_id,
        "slack_team_id": team_id,
        "client_name": inp.client_name,
        "fye_month": inp.fye_month,
        "accounting_software": inp.accounting_software,
        "gst_registered": inp.gst_registered,
        "region": inp.region,
        "base_currency": base_currency,
        "status": "pending_coa",
        "category_mapping": {},
    }
