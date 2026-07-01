"""Slack Bolt app construction + FastAPI / socket-mode entrypoints."""

from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
from typing import Any, Optional

from fastapi import Request, Response

from ledgr_slack.client_context import FirestoreClientStore

from ledgr_slack.batch_coordinator import handle_message_file_upload
from ledgr_slack.client_store import (
    _reply_document_only,
    deslugify_channel_name,
)
from ledgr_slack.dedup import _seen
from ledgr_slack.ledger_store import SlackLedgerStore
from ledgr_slack.sessions import FirestoreSessionService
from ledgr_slack.ux import _strip_slack_mentions

logger = logging.getLogger(__name__)

def build_runner(*, session_service=None, artifact_service=None, direct_document: bool = True):
    """Construct the ADK ``Runner`` bound to ``ledgr_app`` + Firestore sessions.

    File uploads run through the lean ``ledgr_agent`` tools (``read_doc`` +
    ``build_sheets``). Imports are deferred so importing this module never
    touches the network.
    """
    from ledgr_slack.slack_shell import build_ledgr_runner

    logger.info("build_runner: using ledgr_app (lean agent, ADR-0026)")
    return build_ledgr_runner(
        session_service=session_service,
        artifact_service=artifact_service,
    )

def _setup_channel_id(body: dict) -> str:
    """Resolve the channel id from a button-action body (mirrors handle_setup_open)."""
    return (
        body.get("container", {}).get("channel_id")
        or body.get("channel", {}).get("id")
        or body.get("channel_id")
        or ""
    )

async def _derive_setup_prefill(slack_client: Any, body: dict) -> Optional[dict]:
    """Build the onboarding-modal prefill from the channel name (best-effort).

    The channel is already named after the client (e.g. ``sample-channel-client-pte-ltd``),
    so we look up its name via ``conversations_info`` and de-slugify it into a
    ``client_name`` prefill. Returns ``None`` when the name can't be resolved, so
    the modal simply opens empty (a lookup failure must not block setup).
    """
    channel_id = _setup_channel_id(body)
    if not channel_id:
        return None
    try:
        resp = await asyncio.to_thread(
            slack_client.conversations_info, channel=channel_id
        )
    except Exception:  # noqa: BLE001 - prefill is a convenience, never block setup
        logger.exception("conversations_info failed for %s", channel_id)
        return None
    data = resp.data if hasattr(resp, "data") else resp
    channel = data.get("channel") if isinstance(data, dict) else None
    raw_name = channel.get("name") if isinstance(channel, dict) else None
    client_name = deslugify_channel_name(raw_name or "")
    if not client_name:
        return None
    return {"client_name": client_name}

def build_async_app(
    *,
    runner,
    ledger_store: SlackLedgerStore,
    db: Any,
    store=None,
    bot_token: Optional[str] = None,
    installation_store=None,
    state_store=None,
):
    """Build the Bolt ``AsyncApp`` wired to document upload + onboarding handlers.

    Onboarding (``member_joined_channel`` / ``/ledgr`` + settings modal) reuses
    the parked synchronous handlers from ``app.slack_app`` via thread offload.

    Text and @mentions receive a document-only reply (chat Q&A archived per ADR-0032).

    ``store`` defaults to :class:`FirestoreClientStore` so onboarding writes
    end up in the SAME Firestore the pipeline reads — keeps the socket-mode
    path consistent with the FastAPI path.
    """
    from slack_bolt.async_app import AsyncApp

    from app.commands import ledgr_slash_command_name
    from app.slack_app import (
        handle_ledgr_command,
        handle_member_joined,
        handle_onboarding_submit,
        handle_setup_open,
    )

    if store is None:
        from ledgr_slack.client_context import FirestoreClientStore
        store = FirestoreClientStore()

    app_name = runner.app_name
    token = bot_token or os.environ.get("SLACK_BOT_TOKEN")

    # Multi-workspace OAuth (distribution) vs single-token (socket/dev) mode.
    # When the full OAuth config is present (SLACK_CLIENT_ID/SECRET,
    # SLACK_SIGNING_SECRET, SLACK_BASE_URL — see app.config.missing_slack_oauth),
    # build the app in OAuth mode so other firms can self-install via the public
    # "Add to Slack" link. Otherwise fall back to the bot-token mode the socket
    # path + tests rely on (the socket entrypoint strips the OAuth env vars first,
    # so this branch is never taken there). The Firestore-backed stores' ``_db()``
    # is lazy, so constructing the defaults touches no network.
    import app.config as _app_config

    if not _app_config.missing_slack_oauth():
        from app.installation_store import (
            FirestoreInstallationStore,
            FirestoreOAuthStateStore,
        )
        from app.slack_app import BOT_SCOPES
        from slack_bolt.oauth.async_oauth_settings import AsyncOAuthSettings

        settings = _app_config.get_settings()
        if installation_store is None:
            installation_store = FirestoreInstallationStore()
        if state_store is None:
            state_store = FirestoreOAuthStateStore()
        # NOTE: bolt-python's AsyncOAuthSettings has NO ``state_secret`` kwarg
        # (that is a bolt-js concept). The state CSRF value is issued + verified
        # by the ``state_store`` together with a signed browser cookie; no
        # separate secret is accepted here. (Confirmed against the installed
        # slack_bolt/oauth/async_oauth_settings.py signature.)
        async_app = AsyncApp(
            signing_secret=settings.slack_signing_secret,
            oauth_settings=AsyncOAuthSettings(
                client_id=settings.slack_client_id,
                client_secret=settings.slack_client_secret,
                scopes=BOT_SCOPES,
                installation_store=installation_store,
                state_store=state_store,
                install_path="/slack/install",
                redirect_uri_path="/slack/oauth_redirect",
            ),
        )
    else:
        async_app = AsyncApp(token=token)

    # Bolt hands async handlers an AsyncWebClient, but ALL our downstream Slack Web
    # API calls (files_info, chat_postMessage, reactions, files_upload_v2,
    # files_delete, conversations_info) + the parked sync handlers are written
    # synchronously. Use one sync WebClient (same bot token) for every Web API call;
    # the async `client` is only used for Bolt's `ack()`. This is why uploads
    # silently did nothing before — sync calls on the async client returned
    # un-awaited coroutines.
    from slack_sdk import WebClient as _SyncWebClient

    def _sync_client_for(context=None, client=None):
        """Per-workspace sync WebClient. OAuth mode: context['bot_token'] is the
        installing workspace's token (Bolt authorize via installation_store);
        token mode: the single bot token. Falls back to the injected client's
        token, then the build-time token.

        This is the multi-workspace correctness fix: every listener rebinds its
        local ``sync_client`` from THIS, so an event from firm A never posts with
        firm B's token (the previous single global client did).
        """
        # Only accept a real string token. Bolt sets context['bot_token'] (and the
        # injected client's .token) to the per-workspace token; guarding on str
        # avoids building a live WebClient from a non-string (e.g. a test
        # MagicMock's auto-attribute), which would otherwise make real network
        # calls. Falls through to the build-time ``token``.
        tok = None
        if context is not None:
            ctx_tok = context.get("bot_token")
            if isinstance(ctx_tok, str):
                tok = ctx_tok
        if not tok and client is not None:
            client_tok = getattr(client, "token", None)
            if isinstance(client_tok, str):
                tok = client_tok
        return _SyncWebClient(token=tok or token)

    @async_app.event("file_shared")
    async def _file_shared(event, body, client, context=None):
        eid = body.get("event_id") or f"{event.get('type')}:{event.get('event_ts') or event.get('ts')}"
        if _seen.seen_before(eid):
            logger.debug("dedup: dropping duplicate file_shared event %s", eid)
            return
        file_id = event.get("file_id") or event.get("file", {}).get("id")
        channel_id = event.get("channel_id") or event.get("channel")
        if not file_id or not channel_id:
            return
        logger.debug(
            "file_shared: deferring document %s to message/file_share handler for channel %s",
            file_id, channel_id,
        )

    # --- dedup callout card action handlers ---

    @async_app.action("ledgr_dedup_replace")
    async def _dedup_replace(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        await ack()
        action_value = (body.get("actions") or [{}])[0].get("value") or ""
        channel_id_dr = (body.get("channel") or {}).get("id") or ""
        message_ts_dr = (body.get("message") or {}).get("ts") or ""
        try:
            parts = action_value.split("|", 3)
            vendor_raw = urllib.parse.unquote(parts[0]) if len(parts) > 0 else ""
            month_raw = urllib.parse.unquote(parts[2]) if len(parts) > 2 else ""
            stash_key = (
                urllib.parse.unquote(parts[3])
                if len(parts) > 3 and parts[3] not in ("", "-")
                else ""
            )
        except Exception:  # noqa: BLE001
            vendor_raw, month_raw, stash_key = "", "", ""
        label = f"{month_raw} · {vendor_raw}" if (vendor_raw and month_raw) else action_value

        replaced = False
        if stash_key and isinstance(ledger_store, SlackLedgerStore):
            try:
                stash = await asyncio.to_thread(
                    ledger_store.consume_bank_dedup_replace, stash_key,
                )
                if stash and stash.get("batches"):
                    doc_keys = [
                        str(b.get("doc_key") or "")
                        for b in stash["batches"]
                        if b.get("doc_key")
                    ]
                    await asyncio.to_thread(
                        ledger_store.purge_seen_doc_keys,
                        stash["client_id"],
                        stash["fy"],
                        doc_keys,
                    )
                    append_result = await asyncio.to_thread(
                        ledger_store.append_rows,
                        client_id=stash["client_id"],
                        fy=stash["fy"],
                        slack_client=sync_client,
                        channel_id=channel_id_dr,
                        batches=stash["batches"],
                        software=stash.get("software") or "",
                        kind=stash.get("kind") or "bank",
                        client_name=stash.get("client_name") or "",
                    )
                    replaced = int(append_result.get("appended") or 0) > 0
            except Exception:  # noqa: BLE001
                logger.exception("dedup_replace: bank re-merge failed stash=%s", stash_key)

        if channel_id_dr and message_ts_dr:
            try:
                if replaced:
                    outcome = (
                        f"✅ Replaced {label} in your bank statement workbook."
                    )
                    sync_client.chat_update(
                        channel=channel_id_dr,
                        ts=message_ts_dr,
                        text=outcome,
                        blocks=[
                            {
                                "type": "section",
                                "text": {"type": "mrkdwn", "text": outcome},
                            }
                        ],
                    )
                else:
                    sync_client.chat_postEphemeral(
                        channel=channel_id_dr,
                        user=(body.get("user") or {}).get("id") or "",
                        text=(
                            f"Will replace {label} — re-upload the file "
                            "to trigger re-processing."
                        ),
                    )
            except Exception:  # noqa: BLE001
                logger.debug("dedup_replace: could not post outcome")

    @async_app.action("ledgr_dedup_keep")
    async def _dedup_keep(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        # No-op on the ledger. Edit the dedup card in-place to a kept-existing outcome line.
        await ack()
        action_value = (body.get("actions") or [{}])[0].get("value") or ""
        channel_id_dk = (body.get("channel") or {}).get("id") or ""
        message_ts_dk = (body.get("message") or {}).get("ts") or ""
        try:
            parts = action_value.split("|", 3)
            vendor_raw = urllib.parse.unquote(parts[0]) if len(parts) > 0 else ""
            month_raw = urllib.parse.unquote(parts[2]) if len(parts) > 2 else ""
        except Exception:  # noqa: BLE001
            vendor_raw, month_raw = "", ""
        label = f"{month_raw} · {vendor_raw}" if (vendor_raw and month_raw) else "existing entry"
        outcome_text = f"✅ Kept existing — {label} unchanged."
        if channel_id_dk and message_ts_dk:
            try:
                sync_client.chat_update(
                    channel=channel_id_dk,
                    ts=message_ts_dk,
                    text=outcome_text,
                    blocks=[
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": outcome_text},
                        }
                    ],
                )
            except Exception:  # noqa: BLE001
                logger.debug("dedup_keep: could not update message")

    # --- onboarding + commands (reuse parked sync handlers off-thread) ---

    @async_app.action("ledgr_setup_open")
    async def _setup_open(body, ack, client, context=None):
        sync_client = _sync_client_for(context, client)
        await ack()
        prefill = await _derive_setup_prefill(sync_client, body)
        await asyncio.to_thread(
            handle_setup_open, body, lambda *a, **k: None, sync_client, prefill
        )

    @async_app.view("ledgr_onboarding")
    async def _onboarding(body, ack, client, context=None):
        sync_client = _sync_client_for(context, client)
        await ack()
        await asyncio.to_thread(
            handle_onboarding_submit,
            body,
            lambda *a, **k: None,
            sync_client,
            store,
            lambda: "client-" + os.urandom(6).hex(),
        )

    @async_app.command(ledgr_slash_command_name())
    async def _ledgr(ack, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        await ack()
        await asyncio.to_thread(
            handle_ledgr_command, lambda *a, **k: None, body, sync_client, store
        )

    @async_app.event("member_joined_channel")
    async def _member_joined(event, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        eid = body.get("event_id") or f"{event.get('type')}:{event.get('event_ts') or event.get('ts')}"
        if _seen.seen_before(eid):
            logger.debug("dedup: dropping duplicate member_joined_channel event %s", eid)
            return
        bot_user_id = (context or {}).get("bot_user_id") or ""
        await asyncio.to_thread(handle_member_joined, body, None, sync_client, bot_user_id)

    @async_app.event("app_home_opened")
    async def _app_home_opened(event, body, client, context=None):
        from ledgr_slack.app_home import publish_app_home
        from ledgr_slack.credit_adapter import wire_shared_credit_service

        sync_client = _sync_client_for(context, client)
        wire_shared_credit_service()
        user_id = str(event.get("user") or "").strip()
        team_id = str(body.get("team_id") or event.get("team") or "").strip()
        if not user_id or not team_id:
            return
        await publish_app_home(slack_client=sync_client, user_id=user_id, firm_id=team_id)

    @async_app.action("ledgr_credits_refresh")
    async def _credits_refresh(ack, body, client, context=None):
        from ledgr_slack.app_home import publish_app_home
        from ledgr_slack.credit_adapter import wire_shared_credit_service

        sync_client = _sync_client_for(context, client)
        await ack()
        wire_shared_credit_service()
        user_id = str(body.get("user", {}).get("id") or "").strip()
        team_id = str(body.get("team", {}).get("id") or "").strip()
        if not user_id or not team_id:
            return
        await publish_app_home(slack_client=sync_client, user_id=user_id, firm_id=team_id)

    # --- text-question + file-upload handler ---

    @async_app.event("app_mention")
    async def _app_mention(event, body, client, context=None):
        """Handle @Ledgr mentions explicitly (Slack also sends message.channels)."""
        sync_client = _sync_client_for(context, client)
        eid = body.get("event_id") or f"app_mention:{event.get('ts')}"
        if _seen.seen_before(eid):
            return
        channel_id = event.get("channel")
        if not channel_id:
            return
        thread_ts = event.get("thread_ts") or event.get("ts")
        _reply_document_only(sync_client, channel_id, thread_ts=thread_ts)

    @async_app.event("message")
    async def _message(event, body, client, context=None):
        sync_client = _sync_client_for(context, client)
        # Dedup: Slack socket-mode can redeliver the same event on reconnect.
        # One guard per message event_id covers both the file and text paths so
        # a redelivery of a file_share message is suppressed exactly once.
        eid = body.get("event_id") or f"{event.get('type')}:{event.get('ts')}"
        if _seen.seen_before(eid):
            logger.debug("dedup: dropping duplicate message event %s", eid)
            return

        # Ignore bot messages and edit/delete noise — but still process file
        # uploads posted by this app (files.upload / API tests carry bot_id).
        subtype = event.get("subtype") or ""
        files = event.get("files") or []
        is_file_upload = subtype == "file_share" or bool(files)
        if subtype in ("message_changed", "message_deleted"):
            return
        if subtype == "bot_message" and not is_file_upload:
            return
        if event.get("bot_id") and not is_file_upload:
            return

        channel_id = event.get("channel")
        if not channel_id:
            return

        user_hint = _strip_slack_mentions(event.get("text") or "")

        # File-upload path: message subtype "file_share" OR event carries a
        # "files" list (some Slack app configurations omit the subtype but still
        # include the files array).  Process each file independently; the shared
        # _seen guard above already prevents double-processing if the same
        # event_id is redelivered.
        if subtype == "file_share" or files:
            await handle_message_file_upload(
                event=event,
                sync_client=sync_client,
                runner=runner,
                ledger_store=ledger_store,
                db=db,
                app_name=app_name,
                store=store,
                user_hint=user_hint,
                channel_id=channel_id,
                files=files,
                subtype=subtype,
            )
            return

        # Plain text (no files): document-only bot — chat Q&A archived per ADR-0032.
        if not user_hint:
            return
        thread_ts = event.get("thread_ts") or event.get("ts")
        _reply_document_only(sync_client, channel_id, thread_ts=thread_ts)

    return async_app

def build_fastapi_app():
    """Build a FastAPI app that delegates POST /slack/events to the ADK graph.

    Mirrors ``_main_async`` wiring (runner + db + ledger_store + build_async_app
    + FirestoreClientStore) but for the HTTP path used by Cloud Run production.
    Does NOT strip OAuth env vars (that is socket-mode only); production uses
    multi-workspace OAuth via Bolt's OAuthSettings.

    All network/store construction is LAZY (deferred to first request via
    _get_handler) so importing this module never touches the network.

    Route annotations use the module-level ``Request`` / ``Response`` names
    (imported at the top of this file) so FastAPI can resolve them even under
    ``from __future__ import annotations`` (PEP 563 stringifies all annotations;
    FastAPI resolves them against the module globals at decoration time).
    """
    from ledgr_slack.observability import init_sentry_if_configured
    from fastapi import FastAPI

    init_sentry_if_configured()
    from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler

    # All heavyweight objects are deferred to first request. Imports happen
    # inside _get_handler so that test patches applied to the source modules
    # (e.g. accounting_agents.sessions.FirestoreSessionService) are still active
    # at call time — the closure references the source by name, not a captured
    # value that was already resolved at build_fastapi_app() call time.
    _state: dict = {}

    def _get_handler():
        if "handler" not in _state:
            from ledgr_slack.sessions import FirestoreSessionService
            from ledgr_slack.client_context import FirestoreClientStore
            db = FirestoreSessionService().client
            runner = build_runner()
            ledger_store = SlackLedgerStore(db)
            async_app = build_async_app(
                runner=runner,
                ledger_store=ledger_store,
                db=db,
                store=FirestoreClientStore(),
            )
            _state["handler"] = AsyncSlackRequestHandler(async_app)
        return _state["handler"]

    api = FastAPI(title="Ledgr Slack Bot")

    @api.get("/healthz")
    async def healthz():
        import json
        from app.config import missing_slack_http, missing_slack_oauth
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
        return await _get_handler().handle(req)

    return api

async def _main_async() -> None:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler


    # Socket mode is the local/dev single-workspace path: authenticate with
    # SLACK_BOT_TOKEN directly. Bolt auto-enables the OAuth installation store
    # whenever SLACK_CLIENT_ID/SECRET are present in the environment (it checks
    # `is not None`, so even empty strings count), which would make it ignore the
    # bot token. Strip them here — AFTER all imports have run their .env loading —
    # so the AsyncApp built below uses the bot token. Multi-workspace OAuth is the
    # job of the FastAPI/Cloud Run entrypoint, not socket mode.
    for _k in ("SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET", "SLACK_OAUTH_STATE_SECRET"):
        os.environ.pop(_k, None)

    db = FirestoreSessionService().client
    runner = build_runner()
    ledger_store = SlackLedgerStore(db)
    # Onboarding/commands must write to the SAME Firestore the document pipeline
    # reads (_DEFAULT_CLIENT_STORE). Without this, build_async_app defaults to an
    # ephemeral InMemoryClientStore and socket-mode-registered profiles would be
    # invisible to processing (soft-gated as "no_profile").
    async_app = build_async_app(
        runner=runner, ledger_store=ledger_store, db=db,
        store=FirestoreClientStore(),
    )

    handler = AsyncSocketModeHandler(async_app, os.environ["SLACK_APP_TOKEN"])
    logger.info("Starting Ledgr ADK Slack runner in socket mode...")
    await handler.start_async()

def main() -> None:
    asyncio.run(_main_async())

