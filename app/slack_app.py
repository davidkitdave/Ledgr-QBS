"""Slack Bolt app wiring for Ledgr onboarding.

All core logic lives in module-level functions (handle_*) so they can be
unit-tested without a running Bolt server or live Slack token.
"""

from __future__ import annotations

import logging
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import Callable, Optional

from app.blocks import (
    export_unavailable_blocks,
    ledgr_help_blocks,
    onboarding_modal,
    profile_summary_blocks,
    welcome_blocks,
)
from app.commands import parse_ledgr_command, settings_prefill
from app.onboarding import parse_modal_state, profile_doc

logger = logging.getLogger(__name__)

# Canonical bot OAuth scopes — the single source of truth shared by
# OAuthSettings (multi-workspace install) and slack/manifest-distributed.json.
BOT_SCOPES = [
    "chat:write",
    "reactions:write",
    "files:read",
    "files:write",
    "channels:history",
    "groups:history",
    "im:history",
    "channels:read",
    "groups:read",
    "commands",
    "app_mentions:read",
    "users:read",
]

# --------------------------------------------------------------------------- #
# Download safety limits
# --------------------------------------------------------------------------- #

MAX_FILE_BYTES = 25 * 1024 * 1024   # 25 MB per file
MAX_FILES_PER_BATCH = 30            # cap files processed from one message
_ALLOWED_DOWNLOAD_HOSTS = ("files.slack.com",)  # plus *.slack.com (see _is_slack_host)


def _is_slack_host(host: str) -> bool:
    """Return True when *host* is files.slack.com or any *.slack.com host."""
    host = (host or "").lower()
    return host == "slack.com" or host.endswith(".slack.com")


class _NoSlackRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect handler that rejects redirects to non-slack.com hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlparse(newurl).hostname or ""
        if not _is_slack_host(host):
            raise urllib.error.HTTPError(
                newurl, code, f"refusing cross-host redirect to {host}", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SLACK_OPENER = urllib.request.build_opener(_NoSlackRedirect())


# --------------------------------------------------------------------------- #
# Slack-retry idempotency (item 1)
# --------------------------------------------------------------------------- #
#
# Slack re-delivers an event if the bot doesn't ack within 3s, which would
# otherwise let one upload produce duplicate ledgers. We dedupe on the
# envelope ``event_id`` using a small bounded in-memory set.
#
# NOTE: This is process-local. A multi-instance Cloud Run deployment must back
# this with Firestore (a ``processed_events/{event_id}`` doc with a TTL) so
# dedup survives across instances and restarts.

_SEEN_EVENTS_CAP = 512


class _SeenEvents:
    """Bounded FIFO set of recently-seen event ids (thread-safe enough for CPython)."""

    def __init__(self, cap: int = _SEEN_EVENTS_CAP):
        self._cap = cap
        self._seen: "OrderedDict[str, None]" = OrderedDict()

    def seen_before(self, event_id: str) -> bool:
        """Record *event_id*; return True if it was already present."""
        if event_id in self._seen:
            # refresh recency so repeated retries stay tracked
            self._seen.move_to_end(event_id)
            return True
        self._seen[event_id] = None
        while len(self._seen) > self._cap:
            self._seen.popitem(last=False)  # FIFO eviction
        return False


_seen_events = _SeenEvents()


# --------------------------------------------------------------------------- #
# Production Slack IO helpers
# --------------------------------------------------------------------------- #

class FileTooLargeError(Exception):
    """Raised when a Slack file exceeds :data:`MAX_FILE_BYTES`."""


def slack_download_file(client, file_id: str, dest_dir: str) -> str:
    """Download a Slack file to *dest_dir* and return the local path.

    Uses ``urllib.request`` from stdlib — no new runtime dependency.
    The bot token is read from ``client.token`` (set by Bolt on the WebClient).

    Hardening:
    - SSRF: the download host must be ``*.slack.com``; cross-host redirects are
      rejected (an opener installed with :class:`_NoSlackRedirect`).
    - Path traversal / collision: the on-disk name is ``{file_id}_{basename}`` and
      is asserted to resolve inside *dest_dir*.
    - Size: files whose reported size exceeds :data:`MAX_FILE_BYTES` raise
      :class:`FileTooLargeError` before any bytes are fetched.
    - Memory: the body is streamed with ``shutil.copyfileobj`` (never ``read()``).
    """
    info = client.files_info(file=file_id)
    file_meta = info["file"]
    url = file_meta["url_private_download"]
    name = file_meta.get("name") or file_id

    # --- size guard (item 5) ---
    size = file_meta.get("size")
    if isinstance(size, (int, float)) and size > MAX_FILE_BYTES:
        raise FileTooLargeError(
            f"file {file_id} is {int(size)} bytes (> {MAX_FILE_BYTES} cap)"
        )

    # --- SSRF guard (item 4) ---
    host = urllib.parse.urlparse(url).hostname or ""
    if not _is_slack_host(host):
        raise ValueError(f"refusing to download from non-slack host: {host!r}")

    # --- path-traversal + collision guard (item 3) ---
    safe = os.path.basename(name or file_id)
    safe = safe.replace("/", "_").replace("\\", "_").strip() or file_id
    dest = os.path.join(dest_dir, f"{file_id}_{safe}")
    real_dest = os.path.realpath(dest)
    if not real_dest.startswith(os.path.realpath(dest_dir) + os.sep):
        raise ValueError(f"unsafe download path for {name!r}")

    token = client.token
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with _SLACK_OPENER.open(req) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh)  # stream — never load whole file in memory
    return dest


def slack_upload_workbook(client, channel_id: str, filename: str, data: bytes, title: str) -> None:
    """Upload an Excel workbook to a Slack channel via files.upload (v2)."""
    client.files_upload_v2(
        channel=channel_id,
        filename=filename,
        file=data,
        title=title,
    )


# --------------------------------------------------------------------------- #
# Spreadsheet detector
# --------------------------------------------------------------------------- #

_SPREADSHEET_EXTS = {"xlsx", "xls", "csv"}


def _is_spreadsheet(f: dict) -> bool:
    """Return True when the Slack file dict looks like a spreadsheet.

    Checks ``filetype`` first (Slack-assigned), then falls back to the
    file ``name`` extension so plain-text CSVs (filetype="text") are caught.
    """
    filetype = (f.get("filetype") or "").lower().strip(".")
    if filetype in _SPREADSHEET_EXTS:
        return True
    name = f.get("name") or ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext in _SPREADSHEET_EXTS


# --------------------------------------------------------------------------- #
# _event_id — idempotency key for Slack message events.
# --------------------------------------------------------------------------- #


def _event_id(body_or_event: dict, event: dict, files: list) -> str:
    """Best-effort idempotency key for a Slack message event.

    Prefers the envelope ``event_id`` (present on the full Bolt ``body``); falls
    back to the message ``client_msg_ts`` and finally to the sorted file ids.
    """
    eid = body_or_event.get("event_id")
    if eid:
        return str(eid)
    cmts = event.get("client_msg_ts") or event.get("ts")
    if cmts:
        return f"ts:{cmts}"
    file_ids = sorted(str(f.get("id", "")) for f in files)
    return "files:" + ",".join(file_ids)


# --------------------------------------------------------------------------- #
# Pure handler functions (importable + testable without a Bolt server)
# --------------------------------------------------------------------------- #

def handle_setup_open(
    body: dict, ack: Callable, client, prefill: dict | None = None
) -> None:
    """Ack the button, open the onboarding modal with private_metadata=channel_id.

    ``prefill`` (optional) is passed straight through to ``onboarding_modal`` to
    pre-populate fields (e.g. a ``client_name`` derived from the channel name by
    the caller). Defaults to ``None`` for backward-compatible callers.
    """
    ack()
    # Prefer container channel (message context), fall back to body channel
    channel_id = (
        body.get("container", {}).get("channel_id")
        or body.get("channel", {}).get("id")
        or body.get("channel_id")
        or ""
    )
    modal = onboarding_modal(prefill)
    modal["private_metadata"] = channel_id
    client.views_open(trigger_id=body["trigger_id"], view=modal)


def handle_onboarding_submit(
    body: dict,
    ack: Callable,
    client,
    store,
    id_factory: Callable[[], str],
) -> None:
    """Ack the modal submit, persist the profile, post summary in channel.

    On edit (channel already has a profile): reuses the existing client_id and
    preserves category_mapping. Status stays or becomes ``active``.
    """
    ack()
    view = body["view"]
    inp = parse_modal_state(view)

    # channel_id is carried via private_metadata set when the modal was opened
    channel_id = view.get("private_metadata") or ""
    team_id = body.get("team", {}).get("id") or body.get("team_id") or ""

    existing = store.get_by_channel(channel_id)
    if existing is not None:
        client_id = existing.client_id
    else:
        client_id = id_factory()

    doc = profile_doc(inp, channel_id=channel_id, team_id=team_id, client_id=client_id)

    if existing is not None:
        doc["status"] = existing.status or "active"
        doc["category_mapping"] = dict(existing.category_mapping or {})

    store.save_profile(doc)
    store.set_channel(channel_id, client_id)

    client.chat_postMessage(channel=channel_id, blocks=profile_summary_blocks(doc))


def handle_ledgr_command(ack: Callable, body: dict, client, store) -> None:
    """Handle the /ledgr slash command.

    Subcommands:
      settings — open the onboarding modal prefilled with existing profile data.
      export   — post the "export unavailable" message (archive removed).
      help     — post usage card (default for unknown subcommands).
    """
    ack()
    channel_id: str = body.get("channel_id") or ""
    cmd = parse_ledgr_command(body.get("text"))

    if cmd.subcommand == "settings":
        existing = store.get_by_channel(channel_id)
        prefill = settings_prefill(existing)
        modal = onboarding_modal(prefill)
        modal["private_metadata"] = channel_id
        client.views_open(trigger_id=body["trigger_id"], view=modal)

    elif cmd.subcommand == "export":
        _handle_export(channel_id=channel_id, client=client)

    elif cmd.subcommand == "profile":
        existing = store.get_by_channel(channel_id)
        if existing is None:
            client.chat_postMessage(
                channel=channel_id,
                text="No client is set up in this channel yet — run */ledgr settings*.",
            )
        else:
            profile = {
                "client_name": existing.client_name,
                "accounting_software": existing.accounting_software,
                "fye_month": existing.fye_month,
                "gst_registered": existing.tax_registered,
            }
            client.chat_postMessage(channel=channel_id, blocks=profile_summary_blocks(profile))

    else:  # "help" or unknown
        client.chat_postMessage(channel=channel_id, blocks=ledgr_help_blocks())


def _handle_export(*, channel_id: str, client) -> None:
    """Implement /ledgr export: post the export-unavailable message.

    GCS archive was removed (ADR-0002). Export is always unavailable;
    the Slack-hosted ledger canvas is the system of record.
    """
    client.chat_postMessage(channel=channel_id, blocks=export_unavailable_blocks())


def handle_member_joined(body: dict, ack: Optional[Callable], client, bot_user_id: str) -> None:
    """Post welcome card when *this* bot joins a channel."""
    # member_joined_channel events don't have an ack, but accept optional for testability
    if ack is not None:
        try:
            ack()
        except Exception:
            pass
    event = body.get("event", {})
    # Only act when the bot itself joined
    if event.get("user") != bot_user_id:
        return
    channel_id = event.get("channel") or ""
    if channel_id:
        client.chat_postMessage(channel=channel_id, blocks=welcome_blocks())
