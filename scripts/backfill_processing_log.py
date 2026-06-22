#!/usr/bin/env python3
"""Backfill the per-client ``processing_log`` from historical ADK document sessions.

Before the runner was wired to call ``_record_processing_log`` (2026-06-15),
deliveries to a channel left no audit trail the chat agent could read. This
script walks the ADK document sessions for a given ``--channel-id`` and
reconstructs a ``processing_log`` entry per session that has terminal /
delivered state, persisting them via ``FirestoreClientStore.append_processing_log``
(merge=True — idempotent on ``file_id``).

Usage:

    python scripts/backfill_processing_log.py \
        --client-id client-8db0d1fc201f \
        --channel-id C0BASC8U551 \
        --app-name accounting_agents_document

The script is safe to re-run; existing entries are left untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")

from accounting_agents.sessions import FirestoreSessionService  # noqa: E402
from invoice_processing.export.client_context import (  # noqa: E402
    FirestoreClientStore,
)

logger = logging.getLogger("backfill_processing_log")


def _coerce_session_state(raw: Any) -> dict:
    """Return a plain dict from a session-like object (or an empty dict)."""
    if raw is None:
        return {}
    state = getattr(raw, "state", None)
    if state is None and isinstance(raw, dict):
        state = raw.get("state")
    if not isinstance(state, dict):
        return {}
    return state


def _extract_entry(file_id: str, state: dict) -> dict:
    """Build a ``processing_log`` entry dict from a session state snapshot."""
    doc_type = str(
        state.get("doc_type") or state.get("_doc_type") or "invoice"
    ).strip().lower()
    extraction_path = str(
        state.get("extraction_path") or "unknown"
    ).strip().lower()
    summary_table = state.get("summary_table") or []
    try:
        row_count = int(state.get("row_count") or len(summary_table) or 0)
    except (TypeError, ValueError):
        row_count = 0
    delivered_at = (
        state.get("delivered_at")
        or state.get("finalized_at")
        or datetime.now(timezone.utc).isoformat()
    )
    return {
        "file_id": file_id,
        "filename": str(
            state.get("source_filename") or state.get("filename") or file_id
        ),
        "doc_type": doc_type,
        "extraction_path": extraction_path,
        "delivered_at": delivered_at,
        "row_count": row_count,
        "fy": str(state.get("fy") or ""),
        "backfilled": True,
    }


async def backfill_channel(
    *,
    client_id: str,
    channel_id: str,
    app_name: str,
    db: Any,
    limit: int = 200,
) -> dict:
    """Backfill processing_log for ``channel_id`` by walking ADK doc sessions.

    Returns a small summary ``{"scanned": N, "backfilled": M, "skipped": K}``.
    """
    store = FirestoreClientStore(client=db)
    svc = FirestoreSessionService(client=db)
    sessions = await svc.list_sessions(app_name=app_name, user_id=channel_id)
    if not sessions:
        logger.info("no ADK sessions for channel=%s app=%s", channel_id, app_name)
        return {"scanned": 0, "backfilled": 0, "skipped": 0}

    scanned = 0
    backfilled = 0
    skipped = 0
    sess_list = list(getattr(sessions, "sessions", sessions) or [])
    for sess_meta in sess_list[:limit]:
        session_id = (
            sess_meta.get("id")
            if isinstance(sess_meta, dict)
            else getattr(sess_meta, "id", None)
        )
        if not session_id or not str(session_id).startswith(f"{channel_id}:"):
            continue
        file_id = str(session_id).split(":", 1)[-1]
        try:
            sess = await svc.get_session(
                app_name=app_name,
                user_id=channel_id,
                session_id=session_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("get_session failed for %s", session_id)
            skipped += 1
            continue
        state = _coerce_session_state(sess)
        if not state:
            skipped += 1
            continue
        # Only backfill sessions that look delivered (have a final state).
        if not any(
            k in state
            for k in (
                "delivered",
                "final_status",
                "summary_table",
                "extraction_path",
            )
        ):
            skipped += 1
            continue
        entry = _extract_entry(file_id, state)
        try:
            store.append_processing_log(
                client_id=client_id, file_id=file_id, entry=entry
            )
            backfilled += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "append_processing_log failed for file_id=%s", file_id
            )
            skipped += 1
        scanned += 1

    logger.info(
        "backfill done: channel=%s scanned=%d backfilled=%d skipped=%d",
        channel_id,
        scanned,
        backfilled,
        skipped,
    )
    return {"scanned": scanned, "backfilled": backfilled, "skipped": skipped}


def _resolve_db() -> Any:
    """Build the Firestore client used by ``FirestoreSessionService`` etc.

    Mirrors the production wiring in ``accounting_agents.slack_runner``; we
    import the helper lazily so the script doesn't crash on a missing
    ``google.cloud`` install in a pure-local environment.
    """
    try:
        from google.cloud import firestore  # type: ignore
    except Exception:  # noqa: BLE001
        logger.error(
            "google-cloud-firestore not installed; run inside the venv."
        )
        raise SystemExit(2)
    return firestore.Client()


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    db = _resolve_db()
    summary = await backfill_channel(
        client_id=args.client_id,
        channel_id=args.channel_id,
        app_name=args.app_name,
        db=db,
        limit=args.limit,
    )
    print(summary)
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--client-id",
        required=True,
        help="Firestore client id (e.g. client-8db0d1fc201f)",
    )
    p.add_argument(
        "--channel-id",
        required=True,
        help="Slack channel id (e.g. C0BASC8U551)",
    )
    p.add_argument(
        "--app-name",
        default="accounting_agents_document",
        help=(
            "ADK app name that owns the per-document sessions "
            "(default: accounting_agents_document)"
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Max sessions to inspect (default: 200)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
