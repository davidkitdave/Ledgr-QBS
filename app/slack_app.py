"""Slack Bolt app wiring for Ledgr onboarding.

All core logic lives in module-level functions (handle_*) so they can be
unit-tested without a running Bolt server or live Slack token.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from slack_bolt import App
from slack_bolt.oauth.oauth_settings import OAuthSettings

from app.blocks import (
    coa_prompt_blocks,
    export_unavailable_blocks,
    ledgr_help_blocks,
    onboarding_modal,
    welcome_blocks,
)
from app.commands import parse_ledgr_command, settings_prefill
from app.coa_ingest import coa_rows_from_file, ingest_coa, standard_coa_rows
from app.installation_store import (
    FirestoreInstallationStore,
    FirestoreOAuthStateStore,
)
from app.onboarding import parse_modal_state, profile_doc
from invoice_processing.export.client_context import InMemoryClientStore

logger = logging.getLogger(__name__)

# Canonical bot OAuth scopes — the single source of truth shared by
# OAuthSettings (multi-workspace install) and slack/manifest-distributed.json.
BOT_SCOPES = [
    "chat:write",
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

# Module-level executor for background file-share work (small pool — IO-bound)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ledgr-share")

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


def handle_file_share(body_or_event: dict, client, store, archive=None) -> None:
    """Guard, dedupe, and dispatch a Slack file-share message event.

    Accepts the full Bolt envelope ``body`` (which carries ``event_id`` for
    idempotency) and unwraps the inner ``event``. For backward compatibility it
    also accepts a bare event dict (no ``event`` key) — common in tests.

    Guards:
    - Ignores duplicate deliveries (Slack retries) via :data:`_seen_events`.
    - Ignores events from bots (including our own uploads — avoids infinite loop).
    - Ignores subtypes other than None / "file_share" (e.g. message_changed,
      message_deleted, bot_message).
    - Ignores messages with no attached files.

    Disambiguation:
    - Spreadsheet files (.xlsx/.xls/.csv) are routed to COA ingest ONLY when the
      channel's client is awaiting a COA (status ``pending_coa``) or has no
      profile yet; an ``active`` client's spreadsheets go to the document pipeline.
    - All other files are routed to ``run_share`` (document processing).

    Each background task owns a dedicated temp dir and removes it in a ``finally``
    block so client PDFs never leak on disk.

    Heavy work is offloaded to the module-level ``_executor`` so the Bolt ack
    happens within Slack's 3-second window.
    """
    # Unwrap the envelope: Bolt passes the full body, tests may pass a bare event.
    event = body_or_event.get("event", body_or_event)

    # Guard: bot's own message (infinite-loop prevention)
    if event.get("bot_id"):
        return
    # Guard: irrelevant subtypes (message_changed/message_deleted/bot_message/…)
    subtype = event.get("subtype")
    if subtype not in (None, "file_share"):
        return
    # Guard: no files attached
    files = event.get("files")
    if not files:
        return

    # Guard: idempotency — skip if we've already handled this event (item 1).
    eid = _event_id(body_or_event, event, files)
    if _seen_events.seen_before(eid):
        logger.info("skipping duplicate file-share event %s", eid)
        return

    channel_id: str = event["channel"]

    def _upload(ch: str, fname: str, data: bytes, title: str) -> None:
        slack_upload_workbook(client, ch, fname, data, title)

    def _say(**kwargs) -> None:
        client.chat_postMessage(channel=channel_id, **kwargs)

    # Cap files-per-message; tell the user about the rest (item 5).
    if len(files) > MAX_FILES_PER_BATCH:
        skipped = len(files) - MAX_FILES_PER_BATCH
        files = files[:MAX_FILES_PER_BATCH]
        _say(
            text=(
                f"I received {len(files) + skipped} files but can only process "
                f"{MAX_FILES_PER_BATCH} at once — the remaining {skipped} were "
                "skipped. Please re-send them in a smaller batch."
            )
        )

    # Decide whether spreadsheets are COA uploads or just documents (item 6).
    resolved = store.get_by_channel(channel_id)
    coa_pending = resolved is None or resolved.status == "pending_coa"

    if coa_pending:
        spreadsheets = [f for f in files if _is_spreadsheet(f)]
        documents = [f for f in files if not _is_spreadsheet(f)]
    else:
        # Active client: a shared CSV/XLSX is a document, not a new COA.
        spreadsheets = []
        documents = list(files)

    # Dispatch COA ingest for each spreadsheet (own tmp dir, cleaned in finally).
    for f in spreadsheets:
        fid = f["id"]

        def _coa_task(file_id=fid):
            task_dir = tempfile.mkdtemp(prefix="ledgr_coa_")
            try:
                local_path = slack_download_file(client, file_id, task_dir)
                run_coa_ingest(
                    channel_id=channel_id,
                    file_path=local_path,
                    store=store,
                    say_fn=_say,
                )
            finally:
                shutil.rmtree(task_dir, ignore_errors=True)

        _executor.submit(_coa_task)

    # Dispatch document processing for non-spreadsheet files (own tmp dir).
    if documents:
        doc_ids: list[str] = [f["id"] for f in documents]

        def _doc_task(ids=doc_ids):
            task_dir = tempfile.mkdtemp(prefix="ledgr_slack_")

            def _download(fid: str) -> str:
                return slack_download_file(client, fid, task_dir)

            try:
                run_share(
                    channel_id=channel_id,
                    file_ids=ids,
                    store=store,
                    download_fn=_download,
                    upload_fn=_upload,
                    say_fn=_say,
                    archive=archive,
                )
            finally:
                shutil.rmtree(task_dir, ignore_errors=True)

        _executor.submit(_doc_task)


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

def build_oauth_settings(
    *,
    installation_store=None,
    state_store=None,
) -> Optional[OAuthSettings]:
    """Build OAuthSettings for multi-workspace install, or None when not configured.

    Reads :func:`app.config.get_settings`. When ``missing_slack_oauth()`` reports
    any missing env var, OAuth is not configured and this returns ``None`` (the
    caller falls back to single-workspace mode).

    The Firestore stores are constructed lazily and never touch the network on
    construction, so building OAuthSettings here is hermetic. Inject fakes via
    ``installation_store`` / ``state_store`` in tests.
    """
    from app.config import get_settings, missing_slack_oauth

    if missing_slack_oauth():
        return None

    settings = get_settings()
    base_url = settings.base_url
    redirect_uri = (
        f"{base_url.rstrip('/')}/slack/oauth_redirect" if base_url else None
    )
    return OAuthSettings(
        client_id=settings.slack_client_id,
        client_secret=settings.slack_client_secret,
        scopes=BOT_SCOPES,
        installation_store=installation_store or FirestoreInstallationStore(),
        state_store=state_store or FirestoreOAuthStateStore(),
        install_path="/slack/install",
        redirect_uri_path="/slack/oauth_redirect",
        redirect_uri=redirect_uri,
    )


def build_app(
    store=None,
    *,
    bot_token: str = "xoxb-test",
    signing_secret: str = "test",
    id_factory: Optional[Callable[[], str]] = None,
    archive=None,
    oauth_settings: Optional[OAuthSettings] = None,
) -> App:
    """Construct and wire a Slack Bolt App.

    Args:
        store: a ProfileStore-compatible object (defaults to InMemoryClientStore).
        bot_token: Slack bot token (use a real one in production). Ignored in
                   OAuth mode (per-team tokens are resolved by the install store).
        signing_secret: Slack signing secret (use a real one in production).
        id_factory: callable returning a new unique client_id string; defaults to
                    ``lambda: "client-" + uuid4().hex[:12]``.
        archive: optional ArchiveStore for GCS archiving and /ledgr export.
                 Defaults to None (archiving disabled, export shows "no ledger" message).
        oauth_settings: when provided, the app is built in multi-workspace OAuth
                 mode (no fixed token; Bolt's InstallationStoreAuthorize resolves
                 the per-team bot token from the install store on each request).
    """
    if store is None:
        store = InMemoryClientStore()
    if id_factory is None:
        id_factory = lambda: "client-" + uuid4().hex[:12]  # noqa: E731

    if oauth_settings is not None:
        # Multi-workspace OAuth mode: no fixed token. Bolt resolves the per-team
        # token via InstallationStoreAuthorize using the install store.
        app = App(
            signing_secret=signing_secret,
            installation_store=oauth_settings.installation_store,
            oauth_settings=oauth_settings,
        )
    else:
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

    # Resolve the bot user id. In OAuth mode Bolt populates a per-request
    # ``context["bot_user_id"]`` (the right token per team), so we MUST prefer it
    # and never call auth_test() at build time (app.client has no token then).
    # In single-token mode we memoize a one-shot auth_test() as a fallback.
    _bot_user_id_cache: dict[str, str] = {}

    def _bot_user_id() -> str:
        if "id" not in _bot_user_id_cache:
            try:
                _bot_user_id_cache["id"] = app.client.auth_test()["user_id"]
            except Exception:
                _bot_user_id_cache["id"] = ""
        return _bot_user_id_cache["id"]

    @app.event("member_joined_channel")
    def _member_joined(body, client, context):
        # Multi-tenant: Bolt resolves the bot user id per request into context.
        # Fall back to a memoized auth_test() only in single-token mode.
        bot_user_id = context.get("bot_user_id") or _bot_user_id()
        handle_member_joined(body, None, client, bot_user_id)

    @app.command("/ledgr")
    def _ledgr_command(ack, body, client):
        handle_ledgr_command(ack, body, client, store, archive=archive)

    @app.event("message")
    def _file_share(body, client):
        # Pass the full envelope ``body`` (carries event_id for idempotency).
        handle_file_share(body, client, store, archive=archive)

    @app.event("file_shared")
    def _file_shared_noop(body, logger):
        # A file upload fires BOTH a ``message``/``file_share`` event (our processing
        # trigger, above) AND a separate ``file_shared`` event. We process from the
        # message event only; this no-op handler just marks ``file_shared`` as handled
        # so Bolt doesn't log "Unhandled request" and we never double-process.
        pass

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
    from slack_bolt.adapter.fastapi import SlackRequestHandler

    from app.config import get_settings, missing_slack_http, missing_slack_oauth

    settings = get_settings()

    _store = store  # capture; real store created lazily below if needed

    def _get_store():
        nonlocal _store
        if _store is None:
            from invoice_processing.export.client_context import FirestoreClientStore
            _store = FirestoreClientStore()
        return _store

    _archive = None  # capture; real archive created lazily below if needed

    def _get_archive():
        nonlocal _archive
        if _archive is None:
            from app.archive import GcsArchiveStore
            _archive = GcsArchiveStore(settings.gcs_bucket or "ledgr-qbs-source-bucket")
        return _archive

    # Select mode: OAuth multi-workspace when fully configured, else single-workspace.
    oauth = build_oauth_settings()
    if oauth is not None:
        logger.info("Slack app: OAuth multi-workspace mode")
        bolt_app = build_app(
            store=_get_store(),
            signing_secret=settings.slack_signing_secret or "test",
            archive=_get_archive(),
            oauth_settings=oauth,
        )
    else:
        # Loud warning at construction when HTTP-mode env vars are missing (item 9).
        _missing_at_build = missing_slack_http()
        if _missing_at_build:
            logger.warning(
                "Slack HTTP mode is misconfigured — missing env vars: %s. "
                "/healthz will report 503 until these are set.",
                ", ".join(_missing_at_build),
            )
        bolt_app = build_app(
            store=_get_store(),
            bot_token=settings.slack_bot_token or "xoxb-test",
            signing_secret=settings.slack_signing_secret or "test",
            archive=_get_archive(),
        )

    handler = SlackRequestHandler(bolt_app)

    api = FastAPI(title="Ledgr Slack Bot")

    @api.get("/healthz")
    async def healthz():
        # Re-check at request time (env may be set after construction). Healthy
        # when EITHER single-workspace HTTP OR multi-workspace OAuth is usable.
        http_missing = missing_slack_http()
        oauth_missing = missing_slack_oauth()
        if http_missing and oauth_missing:
            return Response(
                content=json.dumps({"ok": False, "missing": http_missing}),
                media_type="application/json",
                status_code=503,
            )
        return {"ok": True}

    @api.post("/slack/events")
    async def slack_events(req: Request):
        return await handler.handle(req)

    if oauth is not None:
        @api.get("/slack/install")
        async def slack_install(req: Request):
            return await handler.handle(req)

        @api.get("/slack/oauth_redirect")
        async def slack_oauth_redirect(req: Request):
            return await handler.handle(req)

    return api
