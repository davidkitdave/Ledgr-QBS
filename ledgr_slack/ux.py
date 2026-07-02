"""Slack UX helpers: status messages, delivery cards, batch aggregates."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.blocks import (
    PIPELINE_STAGES,
    _STAGE_TITLES,
    compose_batch_delivery_summary,
    confident_note_block,
    dedup_callout_card,
    delivery_card_blocks,
    ledger_preview_data_table,
    processing_plan_blocks,
)
from ledgr_slack.delivery_notes import format_partial_failure_note
from ledgr_slack.export.exporters import (
    collect_account_flagged_summary,
    decorate_preview_account_flags,
    format_account_flagged_note,
    format_extraction_doc_count_note,
    get_exporter,
    normalize_software_key,
)

from ledgr_agent.billing import _charge_disabled
from ledgr_slack.config import _env_prefix
from ledgr_slack.credit_adapter import (
    batch_credit_footer_block,
    charge_delivery_credits,
    credit_footer_block,
    dedup_credit_footer_block,
    delivery_charge_units,
)
from ledgr_slack.ledger_store import SlackLedgerStore

logger = logging.getLogger(__name__)

DOC_TYPE_KEY = "doc_type"
EXTRACTION_PATH_KEY = "extraction_path"

class _StageState:
    """Tracks ordered per-stage status for a single document run."""

    def __init__(self) -> None:
        self._stages: list[dict] = [
            {
                "task_id": key,
                "title": _STAGE_TITLES[key],
                "status": "pending",
                "output": None,
            }
            for key in PIPELINE_STAGES
        ]

    def _index(self, stage_key: str) -> Optional[int]:
        for i, s in enumerate(self._stages):
            if s["task_id"] == stage_key:
                return i
        return None

    def advance(self, stage_key: str, *, output: str | None = None) -> None:
        """Mark stages before stage_key complete, stage_key in_progress, rest pending."""
        idx = self._index(stage_key)
        if idx is None:
            return
        for i, s in enumerate(self._stages):
            if i < idx:
                s["status"] = "complete"
            elif i == idx:
                s["status"] = "in_progress"
            else:
                s["status"] = "pending"
        if output is not None and idx > 0:
            self._stages[idx - 1]["output"] = output

    def mark_complete(self, *, output: str | None = None) -> None:
        """Mark all stages complete."""
        for s in self._stages:
            s["status"] = "complete"
        if output is not None and self._stages:
            self._stages[-1]["output"] = output

    def mark_failed(self, stage_key: str, error: str) -> None:
        """Mark stage_key failed; stages after it remain pending."""
        idx = self._index(stage_key)
        if idx is None:
            return
        for i, s in enumerate(self._stages):
            if i < idx:
                s["status"] = "complete"
            elif i == idx:
                s["status"] = "failed"
                s["output"] = error

    def set_output(self, stage_key: str, output: str) -> None:
        """Attach or refresh the output line on an in-progress or complete stage."""
        idx = self._index(stage_key)
        if idx is not None:
            self._stages[idx]["output"] = output

    def snapshot(self) -> list[dict]:
        """Return a copy of the current stage list."""
        return [dict(s) for s in self._stages]

def _parse_row_date(s: Optional[str]):
    """Parse common date strings from exporter row dicts."""
    if not s:
        return None

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None

def _month_label_from_rows(rows: list[dict]) -> str:
    """Human month label from bank txn row ``Date`` fields."""
    months: list[tuple[int, int]] = []
    for row in rows:
        d = _parse_row_date(row.get("Date") or "")
        if d is not None:
            months.append((d.year, d.month))
    if not months:
        return ""
    unique = sorted(set(months))
    month_abbr = [
        "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]
    if len(unique) == 1:
        yr, mo = unique[0]
        return f"{month_abbr[mo]} {yr}"
    first_yr, first_mo = unique[0]
    last_yr, last_mo = unique[-1]
    if first_yr == last_yr:
        return f"{month_abbr[first_mo]}–{month_abbr[last_mo]} {first_yr}"
    return f"{month_abbr[first_mo]} {first_yr}–{month_abbr[last_mo]} {last_yr}"


def _invoice_ids_from_batches(batches: list[dict]) -> list[str]:
    """Collect invoice numbers from exporter row dicts in delivery batches."""
    ids: list[str] = []
    for batch in batches or []:
        for row in batch.get("rows") or []:
            if not isinstance(row, dict):
                continue
            inv = (
                row.get("*InvoiceNumber")
                or row.get("Invoice Number")
                or row.get("Reference")
            )
            if inv:
                token = str(inv).strip()
                if token and token not in ids:
                    ids.append(token)
    return ids


def _patch_processing_log_delivery_ts(
    client_store,
    *,
    client_id: str,
    channel_id: str,
    delivery_message_ts: str,
    file_ids: list[str],
    fy: str = "",
    per_file: list[dict] | None = None,
) -> None:
    """Backfill ``delivery_message_ts`` on batch processing_log entries (Phase 2)."""
    if not (client_store and client_id and delivery_message_ts and file_ids):
        return
    by_id: dict[str, dict] = {}
    for item in per_file or []:
        if isinstance(item, dict):
            fid = str(item.get("file_id") or "").strip()
            if fid:
                by_id[fid] = item
    for file_id in file_ids:
        fid = str(file_id or "").strip()
        if not fid:
            continue
        patch: dict = {
            "delivery_message_ts": delivery_message_ts,
            "channel_id": channel_id,
        }
        if fy:
            patch["fy"] = fy
        extra = by_id.get(fid) or {}
        if extra.get("row_count") is not None:
            patch["row_count"] = extra["row_count"]
        inv_ids = extra.get("invoice_ids")
        if inv_ids:
            patch["invoice_ids"] = list(inv_ids)
        try:
            client_store.append_processing_log(
                client_id=client_id, file_id=fid, entry=patch,
            )
        except Exception:  # noqa: BLE001 — best-effort
            logger.debug(
                "processing_log backfill failed client=%s file=%s",
                client_id, fid, exc_info=True,
            )


def _record_processing_log(
    *,
    state: dict,
    payload: dict,
    batches: list[dict],
    append_result: dict,
    client_store,
    delivery_message_ts: Optional[str] = None,
    channel_id: Optional[str] = None,
    invoice_ids: Optional[list[str]] = None,
) -> None:
    """Persist extraction metadata so the chat assistant can introspect deliveries.

    ``delivery_message_ts`` and ``channel_id`` (Phase 2) are the Slack message
    timestamp the delivery card is parented under and the channel it lives in;
    they let the chat lane resolve a thread reply back to the specific batch the
    user is asking about (``raw_thread_ts`` in answer_question).
    """
    client_id = str(payload.get("client_id") or state.get("client_id") or "").strip()
    # File id may be on top-level state (fresh ADK run) OR inside the ledger payload
    # (older session snapshot); accept both so the entry always has a stable id.
    file_id = str(
        state.get("file_id")
        or payload.get("file_id")
        or ""
    ).strip()
    if not client_id or not file_id:
        return

    doc_type = str(state.get(DOC_TYPE_KEY) or payload.get("kind") or "invoice").strip().lower()
    extraction_path = str(state.get(EXTRACTION_PATH_KEY) or "unknown").strip().lower()
    row_count = sum(len(b.get("rows") or []) for b in batches)
    if row_count == 0:
        summary_table = (
            state.get("ledger_summary_table")
            or state.get("summary_table")
            or []
        )
        if isinstance(summary_table, list) and summary_table:
            row_count = len(summary_table)
        else:
            try:
                row_count = int(state.get("row_count") or 0)
            except (TypeError, ValueError):
                row_count = 0
    if not invoice_ids:
        invoice_ids = _invoice_ids_from_batches(batches)
    entry = {
        "file_id": file_id,
        "filename": str(state.get("source_filename") or file_id),
        "doc_type": doc_type,
        "extraction_path": extraction_path,
        "delivered_at": datetime.now(timezone.utc).isoformat(),
        "row_count": row_count,
        "fy": str(payload.get("fy") or append_result.get("fy") or ""),
    }
    # Phase 2: thread-context linkage. Optional keys only — older entries (pre-fix)
    # simply lack them and the resolver skips.
    if delivery_message_ts:
        entry["delivery_message_ts"] = delivery_message_ts
    if channel_id:
        entry["channel_id"] = channel_id
    if invoice_ids:
        entry["invoice_ids"] = list(invoice_ids)
    try:
        client_store.append_processing_log(client_id=client_id, file_id=file_id, entry=entry)
    except Exception:  # noqa: BLE001 — log write is best-effort
        logger.exception(
            "processing_log write failed for client=%s file=%s", client_id, file_id
        )

def _extraction_doc_count_blocks(
    payload: dict,
    *,
    file_label: str | None = None,
) -> list[dict]:
    """WS-2.4 — G3 doc-count context block when extraction metadata exists."""
    if (payload.get("kind") or "invoice") != "invoice":
        return []
    doc_count = payload.get("extracted_doc_count")
    page_count = payload.get("input_page_count")
    if doc_count is None or page_count is None:
        return []
    note = format_extraction_doc_count_note(int(doc_count), int(page_count))
    if not note:
        return []
    if file_label:
        note = f"📄 *{file_label}* — {note}"
    blocks = [confident_note_block(note)]
    partial = payload.get("partial_failure_warnings") or []
    partial_note = format_partial_failure_note(partial)
    if partial_note:
        if file_label:
            partial_note = f"📄 *{file_label}* — {partial_note}"
        blocks.append(confident_note_block(partial_note))
    return blocks

def _post_delivery_card(
    slack_client: Any,
    channel_id: str,
    *,
    summary: str,
    batches: list[dict],
    payload: dict,
    append_result: dict,
    thread_ts: Optional[str] = None,
) -> None:
    """Post one delivery message: summary + ledger preview data_table(s)."""
    workbook_name = append_result.get("filename") or "Ledger.xlsx"
    fy_str = append_result.get("fy") or str(payload.get("fy") or "")
    try:
        fy_int = int(fy_str)
    except (TypeError, ValueError):
        fy_int = 0
    software = str(payload.get("software") or "qbs_ledger")
    preview_blocks: list[dict] = []
    try:
        preview_exporter = get_exporter(software)
    except Exception:  # noqa: BLE001 — preview decoration is cosmetic
        preview_exporter = None
    for batch in batches:
        batch_rows = batch.get("rows") or []
        if not batch_rows:
            continue
        sheet = str(batch.get("sheet") or "Purchase")
        row_doc_type = "sales" if sheet == "Sales" else "purchase"
        preview_rows = batch_rows
        if preview_exporter is not None:
            try:
                preview_rows = decorate_preview_account_flags(
                    batch_rows, preview_exporter, row_doc_type
                )
            except Exception:  # noqa: BLE001 — preview decoration is cosmetic
                logger.warning(
                    "account-flag preview decoration failed (non-fatal)", exc_info=True
                )
        try:
            preview_blocks.extend(
                ledger_preview_data_table(
                    rows=preview_rows,
                    workbook_name=workbook_name,
                    fy=fy_int,
                    sheet=sheet,
                    software=software,
                    channel_id=channel_id,
                )
            )
        except Exception:  # noqa: BLE001 — preview is cosmetic
            logger.warning(
                "ledger preview build failed for sheet %s (non-fatal)", sheet, exc_info=True
            )
    blocks = (
        delivery_card_blocks(summary, preview_blocks)
        if preview_blocks
        else [{"type": "section", "text": {"type": "mrkdwn", "text": summary}}]
    )
    blocks.extend(_extraction_doc_count_blocks(payload))
    # WS-3.4 — surface low-confidence COA picks on QBS/Xero deliveries.
    try:
        if normalize_software_key(software) not in ("autocount", "sql_account"):
            flagged_note = format_account_flagged_note(
                collect_account_flagged_summary(batches)
            )
            if flagged_note:
                blocks.append(confident_note_block(flagged_note))
    except Exception:  # noqa: BLE001 — cosmetic
        logger.warning("account-flagged delivery note failed (non-fatal)", exc_info=True)
    credits_used = append_result.get("credits_used")
    credits_remaining = append_result.get("credits_remaining")
    if isinstance(credits_used, int) and isinstance(credits_remaining, int):
        blocks.append(
            credit_footer_block(
                credits_used=credits_used,
                credits_remaining=credits_remaining,
            )
        )
    kwargs: dict = {"channel": channel_id, "text": summary, "blocks": blocks}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    try:
        slack_client.chat_postMessage(**kwargs)
    except Exception:  # noqa: BLE001 — cosmetic; never break delivery
        logger.warning("delivery card post failed (non-fatal)", exc_info=True)

def _build_batch_aggregate_blocks(
    deferred_items: list[dict],
    channel_id: str,
    *,
    credit_summary: dict | None = None,
) -> tuple[str, list[dict]]:
    """Build the aggregate delivery summary + data-table blocks for a batch.

    Returns ``(summary_text, blocks)``. Caller is responsible for posting
    (single post) or appending (chat.update merging with the job-summary
    message so the whole batch lives in ONE top-level message).
    """
    if not deferred_items:
        return "", []

    sheet_groups: dict[tuple, dict] = {}
    summary_groups: dict[str, dict] = {}
    client_name = ""

    for item in deferred_items:
        payload = item.get("payload") or {}
        batches = item.get("batches") or []
        workbook_name = item.get("workbook_name") or "Ledger.xlsx"
        fy = str(payload.get("fy") or "")
        software = str(payload.get("software") or "")
        kind = str(payload.get("kind") or "invoice")
        client_name = client_name or str(payload.get("client_name") or "")

        sg = summary_groups.setdefault(
            fy,
            {"fy": fy, "software": software, "kind": kind, "n_rows": 0, "n_docs": 0,
             "client_name": client_name},
        )
        sg["n_rows"] += sum(len(b.get("rows") or []) for b in batches)
        # n_docs counts documents, not per-sheet batches — a single doc that
        # splits into Purchase+Sales sheets is one document, not two.
        sg["n_docs"] += 1

        for batch in batches:
            batch_rows = batch.get("rows") or []
            if not batch_rows:
                continue
            sheet = str(batch.get("sheet") or "Purchase")
            key = (fy, sheet, workbook_name, software, kind)
            grp = sheet_groups.setdefault(
                key,
                {"rows": [], "fy": fy, "sheet": sheet, "workbook_name": workbook_name,
                 "software": software},
            )
            grp["rows"].extend(batch_rows)

    summary = compose_batch_delivery_summary(
        groups=list(summary_groups.values()),
        client_name=client_name,
    )
    preview_blocks: list[dict] = []
    for grp in sheet_groups.values():
        try:
            fy_int = int(grp["fy"])
        except (TypeError, ValueError):
            fy_int = 0
        sheet = str(grp.get("sheet") or "Purchase")
        row_doc_type = "sales" if sheet == "Sales" else "purchase"
        preview_rows = grp["rows"]
        try:
            batch_exporter = get_exporter(str(grp.get("software") or ""))
            preview_rows = decorate_preview_account_flags(
                grp["rows"], batch_exporter, row_doc_type
            )
        except Exception:  # noqa: BLE001 — preview decoration is cosmetic
            logger.warning(
                "batch account-flag preview decoration failed (non-fatal)", exc_info=True
            )
        preview_blocks.extend(
            ledger_preview_data_table(
                rows=preview_rows,
                workbook_name=grp["workbook_name"],
                fy=fy_int,
                sheet=grp["sheet"],
                software=grp["software"],
                channel_id=channel_id,
            )
        )

    blocks = (
        delivery_card_blocks(summary, preview_blocks)
        if preview_blocks
        else [{"type": "section", "text": {"type": "mrkdwn", "text": summary}}]
    )

    # AR2 / WS-1.3 — render the per-doc confident note + import-readiness
    # checklist on the batch-aggregate path too. The single-file path
    # (_post_delivery_card) already does this; the multi-file drop is the
    # COMMON path for the user, and previously showed neither a reconcile
    # total nor a readiness note (the batch cards were "blind"). Iterate
    # deferred_items in the order the user dropped them and append a
    # confident_note_block per item that has one. Errors are non-fatal —
    # the cosmetic notes never break delivery.
    for item in deferred_items:
        item_payload = item.get("payload") or {}
        if not item_payload:
            continue
        try:
            _label = (
                item_payload.get("workbook_label")
                or item_payload.get("source_file")
                or None
            )
            blocks.extend(
                _extraction_doc_count_blocks(
                    item_payload,
                    file_label=_label if len(deferred_items) > 1 else None,
                )
            )
        except Exception:  # noqa: BLE001 — notes are cosmetic
            logger.warning(
                "batch per-item note build failed (non-fatal)", exc_info=True,
            )

    if credit_summary:
        used = credit_summary.get("credits_used")
        remaining = credit_summary.get("credits_remaining")
        if isinstance(used, int) and isinstance(remaining, int) and used > 0:
            blocks.append(
                batch_credit_footer_block(
                    credits_used=used,
                    credits_remaining=remaining,
                )
            )
        elif credit_summary.get("all_deduped"):
            blocks.append(dedup_credit_footer_block())

    return summary, blocks

def _bank_batch_dedup_callout(
    deferred_items: list[dict],
    flush_results: list[dict],
    channel_id: str,
) -> tuple[list[dict], str]:
    """Build Replace/Keep dedup card when a bank batch fully deduped at flush.

    Returns ``(blocks, stash_key)`` where ``stash_key`` keys pending replace
    payloads for the ``ledgr_dedup_replace`` action handler.
    """
    if not deferred_items or not flush_results:
        return [], ""
    deduped = sum(int(r.get("deduped") or 0) for r in flush_results)
    appended = sum(int(r.get("appended") or 0) for r in flush_results)
    if deduped == 0 or appended > 0:
        return [], ""

    payload = (deferred_items[0].get("payload") or {})
    if (payload.get("kind") or "invoice") != "bank":
        return [], ""

    batches: list[dict] = []
    for item in deferred_items:
        batches.extend(item.get("batches") or [])
    if not batches:
        return [], ""

    all_rows = [r for b in batches for r in (b.get("rows") or [])]
    txn_rows = [
        r for r in all_rows
        if (r.get("Description") or "") not in ("BALANCE B/F", "TOTALS")
    ]
    month = _month_label_from_rows(txn_rows) or "this month"
    fy_str = str(payload.get("fy") or "0")
    try:
        fy_int = int(fy_str)
    except (TypeError, ValueError):
        fy_int = 0
    vendor = payload.get("client_name") or "bank statement"
    workbook = (flush_results[0].get("filename") or "") if flush_results else ""
    n_incoming = len(txn_rows)
    client_id = str(payload.get("client_id") or "")

    stash_key = f"{client_id}|{channel_id}|{fy_str}|{month}"
    blocks = dedup_callout_card(
        vendor=vendor,
        fy=fy_int,
        month=month,
        existing={"rows": n_incoming, "date_range": month, "workbook": workbook},
        incoming={"rows": n_incoming, "date_range": month, "file_label": workbook},
        op_id=stash_key,
        channel_id=channel_id,
    )
    return blocks, stash_key

def _stash_bank_dedup_replace(
    ledger_store: Any,
    deferred_items: list[dict],
    *,
    stash_key: str,
) -> None:
    """Persist incoming bank batches so Replace can re-merge without re-upload."""
    if not hasattr(ledger_store, "stash_bank_dedup_replace"):
        return
    batches: list[dict] = []
    payload: dict = {}
    for item in deferred_items:
        payload = item.get("payload") or payload
        batches.extend(item.get("batches") or [])
    if not batches:
        return
    ledger_store.stash_bank_dedup_replace(
        stash_key=stash_key,
        client_id=str(payload.get("client_id") or ""),
        fy=str(payload.get("fy") or ""),
        kind=str(payload.get("kind") or "bank"),
        software=str(payload.get("software") or ""),
        client_name=str(payload.get("client_name") or ""),
        batches=batches,
    )

def _post_batch_aggregate_delivery(
    slack_client: Any,
    channel_id: str,
    deferred_items: list[dict],
) -> None:
    """One aggregate delivery card after a multi-file batch completes."""
    summary, blocks = _build_batch_aggregate_blocks(deferred_items, channel_id)
    if not summary:
        return
    try:
        slack_client.chat_postMessage(
            channel=channel_id,
            text=summary,
            blocks=blocks,
        )
    except Exception:  # noqa: BLE001
        logger.warning("batch aggregate delivery post failed (non-fatal)", exc_info=True)

async def _flush_deferred_ledger_writes(
    *,
    ledger_store: SlackLedgerStore,
    slack_client: Any,
    channel_id: str,
    batch_deferred: list[dict],
    firm_id: str | None = None,
) -> tuple[list[dict], dict | None]:
    """Merge stashed ledger payloads across the batch and write the workbook ONCE.

    Each per-doc run in batch mode added a ``deferred_ledger`` to its result
    (carrying its own ``batches`` list, ``payload`` and ``effective_replace``
    flag). We group by ``(client_id, fy, software, kind)`` and call
    :meth:`SlackLedgerStore.append_rows` once per group. The merged
    ``workbook_name`` is then back-patched onto each ``deferred_delivery`` entry
    so the aggregate delivery card references the right file.

    Returns one :meth:`SlackLedgerStore.append_rows` result dict per FY group
    (empty when nothing was stashed). Callers use ``appended`` / ``deduped`` to
    reconcile the Job summary and decide whether to show delivery preview tables.

    Errors here are non-fatal: the delivery message still posts; the workbook
    may simply miss the late write until a later file drop re-runs the same FY.
    """
    # Group deferred payloads by (client_id, fy, software, kind). Each batch
    # carries its own ``fy`` from ``document_sheet_meta``; never merge unlike FYs.
    groups: dict[tuple[str, str, str, str], dict] = {}
    item_group_keys: dict[int, set[tuple[str, str, str, str]]] = {}
    for item_index, item in enumerate(batch_deferred):
        payload = item.get("payload") or {}
        client_id = payload.get("client_id") or "unknown"
        software = payload.get("software") or ""
        kind = payload.get("kind") or "invoice"
        item_keys: set[tuple[str, str, str, str]] = set()
        batches = item.get("batches") or []
        if not batches:
            continue
        for batch in batches:
            fy = str(batch.get("fy") or payload.get("fy") or "unknown")
            key = (client_id, fy, software, kind)
            item_keys.add(key)
            grp = groups.setdefault(
                key,
                {
                    "client_id": client_id,
                    "fy": fy,
                    "software": software,
                    "kind": kind,
                    "client_name": payload.get("client_name") or "",
                    "batches": [],
                    "items": [],
                },
            )
            grp["batches"].append(
                {**batch, "effective_replace": bool(item.get("effective_replace"))}
            )
        item_group_keys[item_index] = item_keys

    if not groups:
        return [], None

    flush_results: list[dict] = []
    batch_credit: dict | None = None
    for grp in groups.values():
        if not grp["batches"]:
            continue
        group_key = (grp["client_id"], grp["fy"], grp["software"], grp["kind"])
        contributors = [
            batch_deferred[idx]
            for idx, keys in item_group_keys.items()
            if group_key in keys
        ]
        try:
            append_result = await asyncio.to_thread(
                ledger_store.append_rows,
                client_id=grp["client_id"],
                fy=grp["fy"],
                slack_client=slack_client,
                channel_id=channel_id,
                batches=grp["batches"],
                software=grp["software"],
                kind=grp["kind"],
                client_name=grp["client_name"],
                replace=False,
            )
        except Exception:  # noqa: BLE001 — non-fatal; delivery card still posts
            logger.exception(
                "batch-end workbook append failed for client=%s fy=%s kind=%s",
                grp["client_id"], grp["fy"], grp["kind"],
            )
            flush_results.append(
                {
                    "appended": 0,
                    "deduped": 0,
                    "flush_failed": True,
                    "client_id": grp["client_id"],
                    "fy": grp["fy"],
                    "kind": grp["kind"],
                }
            )
            continue
        append_result = append_result or {}
        flush_results.append(append_result)
        credit = _charge_deferred_batch_items(
            contributors=contributors,
            append_result=append_result,
            channel_id=channel_id,
            firm_id=firm_id,
        )
        if credit:
            if batch_credit is None:
                batch_credit = {
                    "credits_used": 0,
                    "credits_remaining": credit.get("credits_remaining"),
                    "all_deduped": bool(credit.get("all_deduped")),
                }
            batch_credit["credits_used"] = int(batch_credit.get("credits_used") or 0) + int(
                credit.get("credits_used") or 0
            )
            if credit.get("credits_remaining") is not None:
                batch_credit["credits_remaining"] = credit["credits_remaining"]
            if not credit.get("all_deduped"):
                batch_credit["all_deduped"] = False
        workbook_name = append_result.get("filename") or ""
        if not workbook_name:
            continue
        for item in contributors:
            payload = item.get("payload") or {}
            item_fys = {
                str(batch.get("fy") or payload.get("fy") or "unknown")
                for batch in (item.get("batches") or [])
            }
            if grp["fy"] in item_fys:
                item["workbook_name"] = workbook_name
    return flush_results, batch_credit


def _charge_deferred_batch_items(
    *,
    contributors: list[dict],
    append_result: dict,
    channel_id: str,
    firm_id: str | None,
) -> dict | None:
    """Deduct credits after a successful batch-end flush (ADR-0016)."""
    if not firm_id or not contributors:
        return None

    total_appended = int(append_result.get("appended") or 0)
    all_deduped = total_appended <= 0 and int(append_result.get("deduped") or 0) > 0
    remaining_units = total_appended
    total_used = 0
    last_remaining: int | None = None

    for item in contributors:
        payload = item.get("payload") or {}
        credits_block = item.get("credits") or {}
        should_charge = _charge_disabled() or credits_block.get("credit_status") == "estimated"
        if not should_charge:
            continue

        file_id = str(item.get("file_id") or payload.get("file_id") or "").strip()
        if not file_id:
            continue

        row_count = sum(len(b.get("rows") or []) for b in (item.get("batches") or []))
        if row_count <= 0:
            continue

        if all_deduped or remaining_units <= 0:
            item_append = {"appended": 0, "all_deduped": True}
        elif len(contributors) == 1:
            item_append = append_result
            remaining_units = 0
        else:
            allocated = min(row_count, remaining_units)
            item_append = {"appended": allocated, "all_deduped": allocated <= 0}
            remaining_units -= allocated

        units = delivery_charge_units(
            kind=str(payload.get("kind") or "invoice"),
            payload={**payload, "input_page_count": item.get("input_page_count")},
            append_result=item_append,
            input_page_count=item.get("input_page_count"),
        )
        if units <= 0:
            continue

        charge_result = charge_delivery_credits(
            firm_id=firm_id,
            channel_id=channel_id,
            file_id=file_id,
            kind=str(payload.get("kind") or "invoice"),
            payload={**payload, "input_page_count": item.get("input_page_count")},
            append_result=item_append,
            input_page_count=item.get("input_page_count"),
        )
        if charge_result:
            total_used += int(charge_result.get("credits_used") or 0)
            rem = charge_result.get("credits_remaining")
            if isinstance(rem, int):
                last_remaining = rem

    if all_deduped and total_used == 0:
        return {"credits_used": 0, "credits_remaining": last_remaining, "all_deduped": True}
    if total_used > 0 and last_remaining is not None:
        return {
            "credits_used": total_used,
            "credits_remaining": last_remaining,
            "all_deduped": False,
        }
    return None

def _simple_status_blocks(text: str) -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def _fail_doc(
    slack_client: Any,
    channel_id: str,
    *,
    source_filename: str,
    status_headline: str,
    user_message: str,
    return_status: str,
    stage_state: _StageState | None = None,
    stage_key: str = "understand",
    stage_error: str = "",
    status_ts: str | None = None,
    upload_msg_ts: str | None = None,
    thread_ts: str | None = None,
    use_plan_blocks: bool = False,
    swap_reactions: bool = True,
    status_callback: Callable[[dict], None] | None = None,
    batch_stage: str | None = None,
    batch_detail: str | None = None,
    file_id: str | None = None,
    extra_return: dict | None = None,
) -> dict:
    """Shared Slack UX for a failed document run (status, message, reactions)."""
    if stage_state is not None and stage_error:
        stage_state.mark_failed(stage_key, stage_error)

    if use_plan_blocks and stage_state is not None:
        blocks = _plan_status_blocks(stage_state, source_filename, channel_id)
    else:
        blocks = _simple_status_blocks(status_headline)

    _update_status(
        slack_client,
        channel_id,
        status_ts,
        status_headline,
        blocks=blocks,
    )
    _post_message(slack_client, channel_id, user_message, thread_ts=thread_ts)

    if swap_reactions:
        _remove_reaction(slack_client, channel_id, upload_msg_ts, "eyes")
        _add_reaction(slack_client, channel_id, upload_msg_ts, "x")

    if status_callback is not None and batch_stage is not None:
        status_callback(
            {
                "file_label": source_filename,
                "stage": batch_stage,
                "detail": batch_detail if batch_detail is not None else user_message,
                "status": "failed",
            }
        )

    result: dict = {"status": return_status, "channel_id": channel_id}
    if file_id is not None:
        result["file_id"] = file_id
    if extra_return:
        result.update(extra_return)
    return result


def _post_message(slack_client: Any, channel_id: str, text: str, thread_ts=None) -> None:
    kwargs = {"channel": channel_id, "text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    slack_client.chat_postMessage(**kwargs)

def _resolve_file_message_ts(slack_client: Any, file_id: str, channel_id: str) -> Optional[str]:
    """Return the ts of the user's upload message for ``file_id`` in ``channel_id``.

    ``files_info`` returns ``file.shares.{public,private}[channel_id][0].ts``.
    Handles both share buckets; returns ``None`` on any error or missing data so
    callers can fall back gracefully — this is cosmetic, never blocking.
    """
    try:
        resp = slack_client.files_info(file=file_id)
        data = resp.data if hasattr(resp, "data") else resp
        if not isinstance(data, dict):
            return None
        file_obj = data.get("file") or {}
        shares = file_obj.get("shares") or {}
        for bucket in ("private", "public"):
            channel_shares = (shares.get(bucket) or {}).get(channel_id)
            if channel_shares:
                ts = channel_shares[0].get("ts")
                if ts:
                    return ts
    except Exception:  # noqa: BLE001 - cosmetic
        logger.debug("files_info failed for file %s channel %s", file_id, channel_id)
    return None

def _resolve_file_channel(
    slack_client: Any, file_id: str
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(channel_id, upload_ts)`` for ``file_id`` from ``files_info``.

    ``file.shares.{public,private}`` is a dict keyed by CHANNEL id, each value a
    list of share records carrying the upload ``ts``.  The proactive re-extract
    view-submission body has no reliable channel context (Slack does not echo the
    source channel on a ``view_submission``), so we recover it from the file's own
    share record — the same ``files_info`` shape :func:`_resolve_file_message_ts`
    reads.  Returns ``(None, None)`` on any error or missing data.
    """
    try:
        resp = slack_client.files_info(file=file_id)
        data = resp.data if hasattr(resp, "data") else resp
        if not isinstance(data, dict):
            return (None, None)
        file_obj = data.get("file") or {}
        shares = file_obj.get("shares") or {}
        for bucket in ("private", "public"):
            channels = shares.get(bucket) or {}
            for channel_id, records in channels.items():
                if records:
                    ts = records[0].get("ts")
                    return (channel_id, ts)
    except Exception:  # noqa: BLE001 - best-effort channel recovery
        logger.debug("files_info(channel) failed for file %s", file_id)
    return (None, None)

def _resolve_file_name(slack_client: Any, file_id: str, file_obj: Optional[dict] = None) -> str:
    """Best-effort REAL uploaded filename for a Slack file.

    The extension drives :func:`_validate_download` and the name labels every
    review card. ``message``/``file_share`` events carry the full file object
    (with ``name``); ``file_shared`` events may not, so we fall back to
    ``files_info``. Returns ``"document.pdf"`` only when the name is truly
    unavailable — NEVER hard-code this elsewhere, or validation always sees a
    supported ``.pdf`` extension and can't reject unsupported uploads (and cards
    all read "document.pdf"). See ADR / QA 2026-06-14.
    """
    name = file_obj.get("name") if isinstance(file_obj, dict) else None
    if not name:
        try:
            resp = slack_client.files_info(file=file_id)
            data = resp.data if hasattr(resp, "data") else resp
            if isinstance(data, dict):
                name = (data.get("file") or {}).get("name")
        except Exception:  # noqa: BLE001 - fall back to default below
            logger.debug("files_info(name) failed for file %s", file_id)
    return name or "document.pdf"

def _add_reaction(slack_client: Any, channel_id: str, ts: Optional[str], name: str) -> None:
    """Add an emoji reaction to a message. Cosmetic: any error is swallowed."""
    if not ts:
        return
    try:
        slack_client.reactions_add(channel=channel_id, timestamp=ts, name=name)
    except Exception as exc:  # noqa: BLE001 - cosmetic
        err = str(exc).lower()
        if "missing_scope" in err or "not_allowed_token" in err:
            logger.warning(
                "reactions_add(%s) blocked (missing reactions:write?) — "
                "reinstall the Slack app from slack/manifest*.json",
                name,
            )
        else:
            logger.debug("reactions_add(%s) failed for %s/%s", name, channel_id, ts)

def _remove_reaction(slack_client: Any, channel_id: str, ts: Optional[str], name: str) -> None:
    """Remove an emoji reaction from a message. Cosmetic: any error is swallowed."""
    if not ts:
        return
    try:
        slack_client.reactions_remove(channel=channel_id, timestamp=ts, name=name)
    except Exception:  # noqa: BLE001 - cosmetic
        logger.debug("reactions_remove(%s) failed for %s/%s", name, channel_id, ts)

def _post_status(
    slack_client: Any,
    channel_id: str,
    text: str,
    thread_ts: Optional[str] = None,
    *,
    blocks: Optional[list] = None,
) -> Optional[str]:
    """Post the initial live-status message and return its ``ts`` (or ``None``).

    Cosmetic-only: a failure here must never abort document processing, so any
    Slack error is logged and swallowed (the run continues silently).
    """
    kwargs: dict = {"channel": channel_id, "text": text}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    if blocks:
        kwargs["blocks"] = blocks
    try:
        resp = slack_client.chat_postMessage(**kwargs)
    except Exception:  # noqa: BLE001 - status post is cosmetic
        logger.exception("failed to post status message in %s", channel_id)
        return None
    data = resp.data if hasattr(resp, "data") else resp
    if isinstance(data, dict):
        return data.get("ts")
    return None

def _plan_status_blocks(
    stage_state: _StageState,
    source_filename: str,
    channel_id: str,
) -> list:
    """Block Kit accordion for live pipeline progress (plan block or fallback)."""
    label = f"{_env_prefix()}`{source_filename}`"
    return processing_plan_blocks(
        label,
        stages=stage_state.snapshot(),
        channel_id=channel_id,
    )

def _update_status(
    slack_client: Any,
    channel_id: str,
    ts: Optional[str],
    text: str,
    *,
    blocks: Optional[list] = None,
) -> None:
    """Edit the live-status message in place. No-op when ``ts`` is missing.

    Cosmetic-only: a failed ``chat_update`` is logged and swallowed so it can
    never crash the run (real processing errors are raised elsewhere).
    """
    if not ts:
        return
    kwargs: dict = {"channel": channel_id, "ts": ts, "text": text}
    if blocks:
        kwargs["blocks"] = blocks
    try:
        slack_client.chat_update(**kwargs)
    except Exception:  # noqa: BLE001 - status update is cosmetic
        logger.exception("failed to update status message in %s", channel_id)

def _strip_slack_mentions(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>", "", text or "").strip()

