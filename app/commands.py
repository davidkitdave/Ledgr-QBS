"""Pure /ledgr slash-command logic — no Slack API calls, fully unit-testable."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LedgrCommand:
    subcommand: str          # "settings" | "export" | "help"
    args: list[str] = field(default_factory=list)


def ledgr_slash_command_name() -> str:
    """Return the slash command this process registers (must match Slack app manifest).

    Production (``LEDGR_ENV=prod``): ``/ledgr`` — Ledgr-QBS on Cloud Run.
    Development (default): ``/ledgr-dev`` — avoids workspace conflict when both
    apps are installed in QBS-AI. Override with ``LEDGR_SLASH_COMMAND``.
    """
    override = (os.environ.get("LEDGR_SLASH_COMMAND") or "").strip()
    if override:
        return override if override.startswith("/") else f"/{override}"
    env = (os.environ.get("LEDGR_ENV") or "dev").strip().lower()
    return "/ledgr-dev" if env == "dev" else "/ledgr"


def parse_ledgr_command(text: str | None) -> LedgrCommand:
    """Parse the text payload from a /ledgr slash command.

    Args:
        text: the text after "/ledgr" from the Slack slash-command body.
              Empty string and None both map to "help".

    Returns:
        LedgrCommand with subcommand one of "settings", "export", "help",
        and args containing the remaining tokens (if any).

    Unknown subcommands fall back to "help".
    """
    raw = (text or "").strip().lower()
    if not raw:
        return LedgrCommand(subcommand="help")

    tokens = raw.split()
    sub = tokens[0]
    args = tokens[1:]

    if sub in ("settings", "export", "help", "profile"):
        return LedgrCommand(subcommand=sub, args=args)

    # unknown subcommand -> help
    return LedgrCommand(subcommand="help", args=tokens)


def settings_prefill(client) -> Optional[dict]:
    """Build an onboarding_modal prefill dict from an existing ClientContext.

    Args:
        client: a ClientContext instance, or None.

    Returns:
        A dict with keys client_name, fye_month (str), accounting_software,
        gst_registered (str "yes"/"no"), or None if client is None.
    """
    if client is None:
        return None

    gst = "yes" if client.tax_registered else "no"
    fye = str(client.fye_month) if client.fye_month is not None else None

    return {
        "client_name": client.client_name or "",
        "fye_month": fye,
        "accounting_software": client.accounting_software or "",
        "gst_registered": gst,
    }
