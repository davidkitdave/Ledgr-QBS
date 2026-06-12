"""Slack Bolt app wiring for Ledgr onboarding.

All core logic lives in module-level functions (handle_*) so they can be
unit-tested without a running Bolt server or live Slack token.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional
from uuid import uuid4

from slack_bolt import App

from app.blocks import (
    coa_prompt_blocks,
    coa_saved_blocks,
    export_unavailable_blocks,
    ledgr_help_blocks,
    onboarding_modal,
    welcome_blocks,
)
from app.commands import parse_ledgr_command, settings_prefill
from app.coa_ingest import coa_rows_from_file, ingest_coa, standard_coa_rows
from app.onboarding import parse_modal_state, profile_doc
from invoice_processing.export.client_context import InMemoryClientStore

# Module-level executor for background file-share work (small pool — IO-bound)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ledgr-share")


# --------------------------------------------------------------------------- #
# Production Slack IO helpers
# --------------------------------------------------------------------------- #

def slack_download_file(client, file_id: str, dest_dir: str) -> str:
    """Download a Slack file to *dest_dir* and return the local path.

    Uses ``urllib.request`` from stdlib — no new runtime dependency.
    The bot token is read from ``client.token`` (set by Bolt on the WebClient).
    """
    info = client.files_info(file=file_id)
    file_meta = info["file"]
    url = file_meta["url_private_download"]
    name = file_meta.get("name") or file_id

    token = client.token
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    dest = os.path.join(dest_dir, name)
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as fh:
        fh.write(resp.read())
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
# File-share handler (pure function + background dispatch)
# --------------------------------------------------------------------------- #

def run_share(
    *,
    channel_id: str,
    file_ids: list[str],
    store,
    download_fn: Callable,
    upload_fn: Callable,
    say_fn: Callable,
    archive=None,
) -> None:
    """Run process_shared_files synchronously — called from the background worker.

    Extracted as a named function so tests can monkeypatch it directly.
    """
    from app.processing import process_shared_files
    process_shared_files(
        channel_id=channel_id,
        file_ids=file_ids,
        store=store,
        download_fn=download_fn,
        upload_fn=upload_fn,
        say_fn=say_fn,
        archive=archive,
    )


def run_coa_ingest(
    *,
    channel_id: str,
    file_path: str,
    store,
    say_fn: Callable,
) -> None:
    """Parse a downloaded spreadsheet as a COA and ingest it.

    Extracted as a named function so tests can monkeypatch it directly.
    """
    rows = coa_rows_from_file(file_path)
    ingest_coa(channel_id=channel_id, store=store, rows=rows, say_fn=say_fn)


def handle_file_share(event: dict, client, store, archive=None) -> None:
    """Guard and dispatch a Slack file-share message event.

    Guards:
    - Ignores events from bots (including our own uploads — avoids infinite loop).
    - Ignores subtypes other than None / "file_share".
    - Ignores messages with no attached files.

    Disambiguation:
    - Spreadsheet files (.xlsx/.xls/.csv) are routed to ``run_coa_ingest``.
    - All other files are routed to ``run_share`` (document processing).
    - If a message contains both types, both paths are dispatched.

    Heavy work is offloaded to the module-level ``_executor`` so the Bolt ack
    happens within Slack's 3-second window.
    """
    # Guard: bot's own message (infinite-loop prevention)
    if event.get("bot_id"):
        return
    # Guard: irrelevant subtypes
    subtype = event.get("subtype")
    if subtype not in (None, "file_share"):
        return
    # Guard: no files attached
    files = event.get("files")
    if not files:
        return

    channel_id: str = event["channel"]

    # Build a per-call temp dir so concurrent shares don't collide.
    tmp_dir = tempfile.mkdtemp(prefix="ledgr_slack_")

    def _download(fid: str) -> str:
        return slack_download_file(client, fid, tmp_dir)

    def _upload(ch: str, fname: str, data: bytes, title: str) -> None:
        slack_upload_workbook(client, ch, fname, data, title)

    def _say(**kwargs) -> None:
        client.chat_postMessage(channel=channel_id, **kwargs)

    spreadsheets = [f for f in files if _is_spreadsheet(f)]
    documents = [f for f in files if not _is_spreadsheet(f)]

    # Dispatch COA ingest for each spreadsheet
    for f in spreadsheets:
        fid = f["id"]

        def _coa_task(file_id=fid):
            local_path = slack_download_file(client, file_id, tmp_dir)
            run_coa_ingest(
                channel_id=channel_id,
                file_path=local_path,
                store=store,
                say_fn=_say,
            )

        _executor.submit(_coa_task)

    # Dispatch document processing for non-spreadsheet files
    if documents:
        doc_ids: list[str] = [f["id"] for f in documents]
        _executor.submit(
            run_share,
            channel_id=channel_id,
            file_ids=doc_ids,
            store=store,
            download_fn=_download,
            upload_fn=_upload,
            say_fn=_say,
            archive=archive,
        )


# --------------------------------------------------------------------------- #
# Pure handler functions (importable + testable without a Bolt server)
# --------------------------------------------------------------------------- #

def handle_setup_open(body: dict, ack: Callable, client) -> None:
    """Ack the button, open the onboarding modal with private_metadata=channel_id."""
    ack()
    # Prefer container channel (message context), fall back to body channel
    channel_id = (
        body.get("container", {}).get("channel_id")
        or body.get("channel", {}).get("id")
        or body.get("channel_id")
        or ""
    )
    modal = onboarding_modal()
    modal["private_metadata"] = channel_id
    client.views_open(trigger_id=body["trigger_id"], view=modal)


def handle_onboarding_submit(
    body: dict,
    ack: Callable,
    client,
    store,
    id_factory: Callable[[], str],
) -> None:
    """Ack the modal submit, persist the profile, post COA prompt in channel.

    On edit (channel already has a profile): reuses the existing client_id,
    preserves the existing status and category_mapping so an edit does not
    reset an active client back to "pending_coa".
    """
    ack()
    view = body["view"]
    inp = parse_modal_state(view)

    # channel_id is carried via private_metadata set when the modal was opened
    channel_id = view.get("private_metadata") or ""
    team_id = body.get("team", {}).get("id") or body.get("team_id") or ""

    # FIX 1: reuse existing client_id/status/category_mapping on edit
    existing = store.get_by_channel(channel_id)
    if existing is not None:
        client_id = existing.client_id
    else:
        client_id = id_factory()

    doc = profile_doc(inp, channel_id=channel_id, team_id=team_id, client_id=client_id)

    if existing is not None:
        doc["status"] = existing.status or "pending_coa"
        doc["category_mapping"] = dict(existing.category_mapping or {})

    store.save_profile(doc)
    store.set_channel(channel_id, client_id)

    client.chat_postMessage(channel=channel_id, blocks=coa_prompt_blocks())


def handle_use_standard_coa(body: dict, ack: Callable, client, store) -> None:
    """Ack the button, ingest the standard SG SME COA for this channel's client."""
    ack()
    channel_id = (
        body.get("container", {}).get("channel_id")
        or body.get("channel", {}).get("id")
        or body.get("channel_id")
        or ""
    )

    def _say(**kwargs) -> None:
        client.chat_postMessage(channel=channel_id, **kwargs)

    ingest_coa(
        channel_id=channel_id,
        store=store,
        rows=standard_coa_rows(),
        say_fn=_say,
    )


def handle_ledgr_command(ack: Callable, body: dict, client, store, archive=None) -> None:
    """Handle the /ledgr slash command.

    Subcommands:
      settings — open the onboarding modal prefilled with existing profile data.
      export   — re-upload the most recent workbook(s) from the archive when
                 available; otherwise post the "no ledger yet" message.
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
        _handle_export(channel_id=channel_id, client=client, store=store, archive=archive)

    else:  # "help" or unknown
        client.chat_postMessage(channel=channel_id, blocks=ledgr_help_blocks())


def _handle_export(*, channel_id: str, client, store, archive=None) -> None:
    """Implement /ledgr export: re-upload the latest workbook(s) from the archive.

    Strategy:
    - If no archive or no client profile → post export_unavailable_blocks().
    - list_workbooks → pick the highest FY; for that FY re-upload every distinct
      workbook filename found there.
    - If list_workbooks returns [] → post export_unavailable_blocks().
    - On success → also post a short confirmation message.
    """
    # No archive wired or no channel profile
    resolved = store.get_by_channel(channel_id) if archive is not None else None
    if archive is None or resolved is None:
        client.chat_postMessage(channel=channel_id, blocks=export_unavailable_blocks())
        return

    workbooks = archive.list_workbooks(resolved.client_id)
    if not workbooks:
        client.chat_postMessage(channel=channel_id, blocks=export_unavailable_blocks())
        return

    # Pick the latest FY
    latest_fy = max(fy for fy, _ in workbooks)
    latest = [(fy, fname) for fy, fname in workbooks if fy == latest_fy]

    # De-duplicate filenames (keep latest fy, which they all share here)
    seen: set[str] = set()
    uploaded_names: list[str] = []
    for fy, filename in latest:
        if filename in seen:
            continue
        seen.add(filename)
        data = archive.get_workbook(resolved.client_id, fy, filename)
        if data is None:
            continue
        slack_upload_workbook(client, channel_id, filename, data, filename)
        uploaded_names.append(filename)

    if uploaded_names:
        names_str = ", ".join(f"`{n}`" for n in uploaded_names)
        client.chat_postMessage(
            channel=channel_id,
            text=f"Re-sent your latest ledger(s): {names_str}",
        )
    else:
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


# --------------------------------------------------------------------------- #
# Bolt App factory
# --------------------------------------------------------------------------- #

def build_app(
    store=None,
    *,
    bot_token: str = "xoxb-test",
    signing_secret: str = "test",
    id_factory: Optional[Callable[[], str]] = None,
    archive=None,
) -> App:
    """Construct and wire a Slack Bolt App.

    Args:
        store: a ProfileStore-compatible object (defaults to InMemoryClientStore).
        bot_token: Slack bot token (use a real one in production).
        signing_secret: Slack signing secret (use a real one in production).
        id_factory: callable returning a new unique client_id string; defaults to
                    ``lambda: "client-" + uuid4().hex[:12]``.
        archive: optional ArchiveStore for GCS archiving and /ledgr export.
                 Defaults to None (archiving disabled, export shows "no ledger" message).
    """
    if store is None:
        store = InMemoryClientStore()
    if id_factory is None:
        id_factory = lambda: "client-" + uuid4().hex[:12]  # noqa: E731

    app = App(
        token=bot_token,
        signing_secret=signing_secret,
        token_verification_enabled=False,
    )

    @app.action("ledgr_setup_open")
    def _setup_open(body, ack, client):
        handle_setup_open(body, ack, client)

    @app.action("ledgr_use_standard_coa")
    def _use_standard_coa(body, ack, client):
        handle_use_standard_coa(body, ack, client, store)

    @app.view("ledgr_onboarding")
    def _onboarding_submit(body, ack, client):
        handle_onboarding_submit(body, ack, client, store, id_factory)

    @app.event("member_joined_channel")
    def _member_joined(body, client):
        # Resolve bot user id from the auth response cached on the app client
        try:
            bot_user_id = app.client.auth_test()["user_id"]
        except Exception:
            bot_user_id = ""
        handle_member_joined(body, None, client, bot_user_id)

    @app.command("/ledgr")
    def _ledgr_command(ack, body, client):
        handle_ledgr_command(ack, body, client, store, archive=archive)

    @app.event("message")
    def _file_share(event, client):
        handle_file_share(event, client, store, archive=archive)

    return app


# --------------------------------------------------------------------------- #
# FastAPI wrapper
# --------------------------------------------------------------------------- #

def fastapi_app(store=None):
    """Create a FastAPI app that mounts the Bolt handler at POST /slack/events.

    Tokens are read from the environment via :func:`app.config.get_settings` at
    call time (never at import time — Cloud Run sets env vars after import).
    Placeholder values keep ``/healthz`` and CI working when env vars are unset;
    real values are used when present.

    A real FirestoreClientStore is constructed lazily (at first request) when
    no store is injected — import-time never touches Firestore.
    """
    from fastapi import FastAPI, Request, Response
    from slack_bolt.adapter.fastapi import SlackRequestHandler

    from app.config import get_settings

    settings = get_settings()

    _store = store  # capture; real store created lazily below if needed

    def _get_store():
        nonlocal _store
        if _store is None:
            from invoice_processing.export.client_context import FirestoreClientStore
            _store = FirestoreClientStore()
        return _store

    from app.archive import GcsArchiveStore
    _archive = GcsArchiveStore(settings.gcs_bucket or "ledgr-qbs-source-bucket")

    bolt_app = build_app(
        store=_get_store(),
        bot_token=settings.slack_bot_token or "xoxb-test",
        signing_secret=settings.slack_signing_secret or "test",
        archive=_archive,
    )
    handler = SlackRequestHandler(bolt_app)

    api = FastAPI(title="Ledgr Slack Bot")

    @api.get("/healthz")
    async def healthz():
        return {"ok": True}

    @api.post("/slack/events")
    async def slack_events(req: Request):
        return await handler.handle(req)

    return api
