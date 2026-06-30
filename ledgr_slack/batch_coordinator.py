"""Batch file-upload fan-out, job progress, and aggregate delivery."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from app.blocks import batch_processing_plan_blocks, job_progress_text, job_summary_text

from ledgr_slack.client_store import _profile_state_delta
from ledgr_slack.dedup import _seen
from ledgr_slack.file_event import (
    download_pdf_bytes,
    process_file_event,
)
from ledgr_slack.ux import (
    _bank_batch_dedup_callout,
    _build_batch_aggregate_blocks,
    _flush_deferred_ledger_writes,
    _invoice_ids_from_batches,
    _patch_processing_log_delivery_ts,
    _post_batch_aggregate_delivery,
    _resolve_file_name,
    _stash_bank_dedup_replace,
)

logger = logging.getLogger(__name__)


async def handle_message_file_upload(
    *,
    event: dict,
    sync_client: Any,
    runner: Any,
    ledger_store: Any,
    db: Any,
    app_name: str,
    store: Any,
    user_hint: str,
    channel_id: str,
    files: list,
    subtype: str,
) -> None:
    """Process a Slack message event that carries one or more file uploads."""
    if subtype == "file_share" or files:
        # ADR-0007: one Job summary message per batch drop, threaded.
        # Post the summary up-front (initial text), pass its ``ts`` as
        # ``thread_ts`` into each ``process_file_event`` so every per-doc
        # status / approval / delivery card lands under it, then edit the
        # summary in-place with the final tally once the loop finishes.

        # Step 5 — Pre-gather de-dup: deduplicate by file id BEFORE fan-out
        # so two list entries sharing an id (file_share + message/file_share
        # dual-fire, or an API test sending duplicates) are processed exactly
        # once.  The _seen.seen_before("file:{id}") guard inside the loop
        # body cannot protect against this under asyncio.gather because two
        # coroutines can both pass the check before either marks the id seen.
        _seen_ids: set[str] = set()
        _deduped_files: list = []
        for _f in files:
            _fid = _f.get("id") if isinstance(_f, dict) else None
            if _fid is None:
                _deduped_files.append(_f)
            elif _fid not in _seen_ids:
                _seen_ids.add(_fid)
                _deduped_files.append(_f)
            else:
                logger.debug("pre-gather dedup: skipping duplicate file id %s", _fid)
        files = _deduped_files

        total = len(files)
        # Per-doc row tracker for the batch plan block. Each entry is a
        # dict {file_label, stage, detail, status} updated by the per-doc
        # status_callback as the run advances. Initial state: queued.
        doc_rows: list[dict] = []
        for f in files:
            fid = f.get("id") if isinstance(f, dict) else None
            fname = _resolve_file_name(sync_client, fid, f) if fid else ""
            doc_rows.append({
                "file_label": fname or f"doc {len(doc_rows) + 1}",
                "stage": "queued",
                "detail": None,
                "status": "in_progress",
            })

        # Post the placeholder summary (top-level, no thread_ts) with the
        # BatchKit ``plan`` block listing every document. Single and
        # multi-file drops use the same plan block UX; this matches the
        # intent of ADR-0007 (one Job summary per drop) and keeps the
        # per-doc "thinking" stages visible in the main channel.
        initial_blocks = batch_processing_plan_blocks(
            total=total,
            done=0,
            doc_rows=list(doc_rows),
            channel_id=channel_id,
        )
        initial_text = job_progress_text(total=total, done=0)
        try:
            post_kwargs: dict = {"channel": channel_id, "text": initial_text}
            if initial_blocks:
                post_kwargs["blocks"] = initial_blocks
            resp = sync_client.chat_postMessage(**post_kwargs)
        except Exception:  # noqa: BLE001 - cosmetic; never abort the upload
            logger.exception("failed to post Job summary in %s", channel_id)
            try:
                resp = sync_client.chat_postMessage(
                    channel=channel_id,
                    text=initial_text,
                )
            except Exception:
                logger.exception("failed to post fallback Job summary in %s", channel_id)
                resp = None
        summary_ts: Optional[str] = None
        if resp is not None:
            data = resp.data if hasattr(resp, "data") else resp
            if isinstance(data, dict):
                summary_ts = data.get("ts")

        posted = 0
        needs_review = 0
        rejected = 0
        failed = 0
        duplicates = 0
        done = 0
        software_hint = ""
        fy_hint = ""
        kind_hint = ""
        # Single-file drops now use the same main-channel UX as multi-file:
        # processing "thinking" stages render on the top-level job summary
        # (batch_processing_plan_blocks), and the delivery preview tables
        # merge into the same top-level final edit. HITL review cards
        # continue to thread under summary_ts (ADR-0007).
        batch_defer = True
        batch_deferred: list[dict] = []
        batch_file_ids: list[str] = []
        _last_progress_refresh = [0.0]
        _PROGRESS_REFRESH_MIN_S = 1.5

        def _refresh_job_progress(*, force: bool = False) -> None:
            if not summary_ts:
                return
            now = time.monotonic()
            if (
                not force
                and done < total
                and (now - _last_progress_refresh[0]) < _PROGRESS_REFRESH_MIN_S
            ):
                return
            _last_progress_refresh[0] = now
            progress_text = job_progress_text(
                total=total,
                done=done,
                posted=posted,
                needs_review=needs_review,
                rejected=rejected,
                failed=failed,
                duplicates=duplicates,
            )
            try:
                # Use the plan block for every drop (single + multi) so the
                # user sees live per-doc thinking on the top-level message.
                blocks = batch_processing_plan_blocks(
                    total=total,
                    done=done,
                    doc_rows=list(doc_rows),
                    channel_id=channel_id,
                )
                sync_client.chat_update(
                    channel=channel_id,
                    ts=summary_ts,
                    text=progress_text,
                    blocks=blocks,
                )
            except Exception:  # noqa: BLE001
                logger.debug("job progress update failed", exc_info=True)
                try:
                    sync_client.chat_update(
                        channel=channel_id,
                        ts=summary_ts,
                        text=progress_text,
                    )
                except Exception:
                    logger.debug("job progress text-only update failed", exc_info=True)

        def _batch_status_cb(update: dict) -> None:
            """Callback fired by process_file_event on each stage change.

            Finds the row matching the doc's filename and updates its
            stage/detail/status in place; then refreshes the placeholder
            with the live plan block. Runs for every drop size — single
            files update the same top-level plan block as multi-file drops.
            """
            label = update.get("file_label") or ""
            for row in doc_rows:
                if row.get("file_label") == label:
                    row.update({
                        "stage": update.get("stage") or row.get("stage"),
                        "detail": update.get("detail"),
                        "status": update.get("status") or row.get("status"),
                    })
                    break
            _refresh_job_progress()

        batch_profile_delta = _profile_state_delta(store, channel_id)

        async def _run_one(f: dict, idx: int) -> dict:
            """Run one doc through the pipeline; return a result record.

            Never raises — the entire body is wrapped so one bad doc never
            cancels sibling coroutines.
            """
            try:
                file_id = f.get("id") if isinstance(f, dict) else None
                if not file_id:
                    return {"idx": idx, "status": "skipped", "file_id": None,
                            "append": {}, "fname": ""}

                # File-level dedup: file_shared + message/file_share both fire
                # for one upload; guard on the file id so it's processed once.
                if _seen.seen_before(f"file:{file_id}"):
                    logger.debug("dedup: file %s already being processed", file_id)
                    return {"idx": idx, "status": "seen_before",
                            "file_id": file_id, "append": {},
                            "fname": ""}

                logger.info(
                    "file upload received via message: file=%s channel=%s",
                    file_id, channel_id,
                )

                fname = _resolve_file_name(sync_client, file_id, f)
                # Update the doc_row for this slot — keyed by idx so two docs
                # with identical filenames (e.g. "document.pdf") each update
                # their own row without colliding on fname.
                if 0 <= idx < len(doc_rows):
                    doc_rows[idx].update({"stage": "Starting…", "status": "in_progress"})
                _refresh_job_progress()

                result = await process_file_event(
                    runner=runner,
                    ledger_store=ledger_store,
                    db=db,
                    slack_client=sync_client,
                    channel_id=channel_id,
                    file_id=file_id,
                    app_name=app_name,
                    download_fn=download_pdf_bytes,
                    thread_ts=summary_ts,
                    source_filename=fname,
                    hint=user_hint,
                    defer_slack_delivery=batch_defer,
                    batch_mode=batch_defer,
                    defer_ledger_persist=batch_defer,
                    status_callback=_batch_status_cb if batch_defer else None,
                    profile_delta=batch_profile_delta,
                )
                return {"idx": idx, "status": (result or {}).get("status", ""),
                        "file_id": file_id,
                        "append": (result or {}).get("append") or {},
                        "fname": fname}

            except Exception as exc:  # noqa: BLE001 — whole-coroutine safety net
                file_id_safe = (f.get("id") if isinstance(f, dict) else None) or ""
                logger.exception("batch file processing failed: file=%s", file_id_safe)
                err_short = str(exc).split("\n", maxsplit=1)[0][:200]
                if 0 <= idx < len(doc_rows):
                    doc_rows[idx].update({
                        "stage": "Processing failed",
                        "detail": err_short,
                        "status": "failed",
                    })
                return {"idx": idx, "status": "processing_failed",
                        "file_id": file_id_safe, "append": {}, "fname": "",
                        "error": err_short}

        # Fan-out: run all docs concurrently.  return_exceptions=True ensures
        # that even if _run_one's outer try/except somehow misses a raise (e.g.
        # a BaseException subclass that bypasses BLE001) the sibling coroutines
        # still complete.  The reduce below converts any leftover Exception
        # objects into processing_failed records.
        _raw_results = await asyncio.gather(
            *[_run_one(f, i) for i, f in enumerate(files)],
            return_exceptions=True,
        )
        _one_results: list[dict] = []
        for _i, _r in enumerate(_raw_results):
            if isinstance(_r, BaseException):
                logger.error(
                    "gather: unexpected exception from _run_one idx=%d: %s", _i, _r,
                    exc_info=_r,
                )
                _one_results.append({
                    "idx": _i, "status": "processing_failed",
                    "file_id": "", "append": {}, "fname": "",
                    "error": str(_r).split("\n", 1)[0][:200],
                })
            else:
                _one_results.append(_r)

        # Post-gather reduce: iterate results in ORIGINAL INPUT ORDER and
        # mutate shared aggregates.  This is the only place shared state is
        # written, so there are no races.
        for _res in sorted(_one_results, key=lambda r: r["idx"]):
            _status = _res.get("status") or ""
            _file_id = _res.get("file_id")
            _append = _res.get("append") or {}

            if _status == "skipped":
                continue

            if _status == "seen_before":
                done += 1
                continue

            # Normal pipeline result.
            done += 1
            if _status == "delivered":
                posted += 1
                if _file_id:
                    batch_file_ids.append(_file_id)
                deferred = _append.get("deferred_delivery")
                if deferred:
                    batch_deferred.append(deferred)
                if not software_hint and _append.get("software"):
                    software_hint = str(_append["software"])
                if not fy_hint and _append.get("fy"):
                    fy_hint = str(_append["fy"])
                if not kind_hint and _append.get("kind"):
                    kind_hint = str(_append["kind"])
            elif _status == "duplicate":
                duplicates += 1
            elif _status == "paused":
                needs_review += 1
            elif _status == "rejected_unreadable":
                rejected += 1
            elif _status == "processing_failed":
                failed += 1

        _refresh_job_progress(force=True)

        # Batch-end: merge stashed ledger payloads and write the workbook ONCE
        # per (client, fy, kind) group — applies to single- and multi-file drops
        # whenever ``defer_ledger_persist`` was used.
        flush_results: list[dict] = []
        if batch_deferred and ledger_store is not None:
            flush_results = await _flush_deferred_ledger_writes(
                ledger_store=ledger_store,
                slack_client=sync_client,
                channel_id=channel_id,
                batch_deferred=batch_deferred,
            )

        ledger_appended = sum(int(r.get("appended") or 0) for r in flush_results)
        ledger_deduped = sum(int(r.get("deduped") or 0) for r in flush_results)

        # Edit the summary in-place with the final tally (ADR-0007). Always
        # merge delivery preview blocks from extracted rows when the batch
        # stashed payloads — independent of whether Firestore deduped at flush.
        if summary_ts:
            try:
                delivery_summary, agg_blocks = (
                    _build_batch_aggregate_blocks(batch_deferred, channel_id)
                    if batch_deferred else ("", [])
                )
                if delivery_summary:
                    final_text = delivery_summary
                    if ledger_deduped > 0 and ledger_appended == 0:
                        final_text += " _(workbook unchanged)_"
                else:
                    final_text = job_summary_text(
                        total=total,
                        posted=posted,
                        needs_review=needs_review,
                        rejected=rejected,
                        failed=failed,
                        duplicates=duplicates,
                        software=software_hint,
                        fy=fy_hint,
                        kind=kind_hint,
                    )
                update_kwargs: dict = {
                    "channel": channel_id,
                    "ts": summary_ts,
                    "text": final_text,
                }
                if batch_deferred and agg_blocks:
                    blocks_out = list(agg_blocks)
                    # Same-period bank re-drop: surface Replace / Keep callout.
                    if (
                        ledger_appended == 0
                        and ledger_deduped > 0
                        and kind_hint == "bank"
                    ):
                        dedup_blocks, stash_key = _bank_batch_dedup_callout(
                            batch_deferred, flush_results, channel_id,
                        )
                        if dedup_blocks:
                            blocks_out.extend(dedup_blocks)
                            if ledger_store is not None and stash_key:
                                _stash_bank_dedup_replace(
                                    ledger_store,
                                    batch_deferred,
                                    stash_key=stash_key,
                                )
                    update_kwargs["blocks"] = blocks_out
                sync_client.chat_update(**update_kwargs)
            except Exception:  # noqa: BLE001 - cosmetic
                logger.exception("failed to update Job summary in %s", channel_id)
                # Never leave the card stuck on "Processing batch …". Slack
                # rejects the rich update (e.g. invalid_blocks when a preview
                # data_table exceeds a block limit), so retry with a
                # plain-text-only summary — no blocks — so the user always
                # gets a clean delivery confirmation.
                try:
                    fallback_text = (
                        final_text
                        if "final_text" in locals() and final_text
                        else job_summary_text(
                            total=total,
                            posted=posted,
                            needs_review=needs_review,
                            rejected=rejected,
                            failed=failed,
                            duplicates=duplicates,
                            software=software_hint,
                            fy=fy_hint,
                            kind=kind_hint,
                        )
                    )
                    logger.warning(
                        "falling back to plain-text Job summary in %s",
                        channel_id,
                    )
                    sync_client.chat_update(
                        channel=channel_id,
                        ts=summary_ts,
                        text=fallback_text,
                    )
                except Exception:  # noqa: BLE001 - cosmetic
                    logger.exception(
                        "failed to update fallback Job summary in %s",
                        channel_id,
                    )
            # Phase 2 backfill: patch delivery_message_ts onto per-doc log
            # entries written during the batch loop (summary_ts is the thread parent).
            if batch_file_ids and store is not None:
                profile = store.get_by_channel(channel_id)
                cid = getattr(profile, "client_id", None) or ""
                if cid:
                    per_file_meta: list[dict] = []
                    for fid, deferred in zip(
                        batch_file_ids, batch_deferred, strict=False,
                    ):
                        batches = (deferred or {}).get("batches") or []
                        per_file_meta.append({
                            "file_id": fid,
                            "row_count": sum(
                                len(b.get("rows") or []) for b in batches
                            ),
                            "invoice_ids": _invoice_ids_from_batches(batches),
                        })
                    _patch_processing_log_delivery_ts(
                        store,
                        client_id=cid,
                        channel_id=channel_id,
                        delivery_message_ts=summary_ts,
                        file_ids=batch_file_ids,
                        fy=fy_hint,
                        per_file=per_file_meta,
                    )
        elif batch_deferred:
            # No summary_ts (rare — placeholder post failed) — fall back to a
            # separate delivery post for backwards-compat.
            _post_batch_aggregate_delivery(
                sync_client, channel_id, batch_deferred,
            )

