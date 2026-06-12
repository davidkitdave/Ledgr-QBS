"""Pure processing core for Slack file-share events.

Receives injected IO callables so the entire module is hermetically testable
without a live Slack token, Gemini call, or network access.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from invoice_processing.pipeline import BatchResult, process_batch


@dataclass
class ShareOutcome:
    channel_id: str
    n_files: int
    workbooks: list[str]      # filenames uploaded
    n_processed: int          # docs with no ERROR note
    errors: list[str]
    status: str               # "ok" | "no_profile" | "no_files" | "error"


def process_shared_files(
    *,
    channel_id: str,
    file_ids: list[str],
    store,
    download_fn: Callable[[str], str],
    upload_fn: Callable[[str, str, bytes, str], None],
    say_fn: Callable,
    pipeline_fn: Callable = process_batch,
    archive=None,
) -> ShareOutcome:
    """Download files, run the pipeline, upload workbooks, post a result card.

    All IO is injected so callers can substitute test doubles without patching.

    Args:
        channel_id:   Slack channel the files were shared to.
        file_ids:     Slack file IDs to process.
        store:        ChannelClientStore — ``get_by_channel(channel_id) -> ClientContext | None``.
        download_fn:  ``(file_id) -> local_path`` — writes the file and returns its path.
        upload_fn:    ``(channel_id, filename, data: bytes, title: str) -> None``.
        say_fn:       ``(blocks=..., text=...) -> None`` — posts to the channel.
        pipeline_fn:  Defaults to ``process_batch``; injectable for tests.
        archive:      Optional ArchiveStore. When provided, source docs and workbooks are
                      archived to GCS. Archiving failures are recorded in errors but never
                      prevent upload or posting the result card. When None, no archiving
                      occurs and behaviour is identical to the previous implementation.
    """
    from app.blocks import needs_setup_blocks, processing_ack_blocks, result_card  # local import avoids circular

    # ------------------------------------------------------------------ #
    # 1. Resolve client profile
    # ------------------------------------------------------------------ #
    client = store.get_by_channel(channel_id)
    if client is None:
        say_fn(
            blocks=needs_setup_blocks(),
            text="This channel isn't set up yet — tap *Set up this client* to get started.",
        )
        return ShareOutcome(
            channel_id=channel_id,
            n_files=len(file_ids),
            workbooks=[],
            n_processed=0,
            errors=[],
            status="no_profile",
        )

    # ------------------------------------------------------------------ #
    # 2. Guard: no files
    # ------------------------------------------------------------------ #
    if not file_ids:
        return ShareOutcome(
            channel_id=channel_id,
            n_files=0,
            workbooks=[],
            n_processed=0,
            errors=[],
            status="no_files",
        )

    # ------------------------------------------------------------------ #
    # 2b. Immediate acknowledgment so the user sees the bot is working (UX).
    #     A failed ack must never block processing.
    # ------------------------------------------------------------------ #
    try:
        say_fn(
            blocks=processing_ack_blocks(len(file_ids)),
            text=f"Got it — processing {len(file_ids)} document(s)…",
        )
    except Exception:  # noqa: BLE001
        pass

    # ------------------------------------------------------------------ #
    # 3. Download files into a temp dir
    # ------------------------------------------------------------------ #
    tmp_dir = tempfile.mkdtemp(prefix="ledgr_")
    try:
        local_paths: list[str] = []
        download_errors: list[str] = []

        for fid in file_ids:
            try:
                path = download_fn(fid)
                local_paths.append(path)
            except Exception as exc:  # noqa: BLE001
                download_errors.append(f"file {fid}: download failed — {exc}")

        # ------------------------------------------------------------------ #
        # 4. Run pipeline
        # ------------------------------------------------------------------ #
        result: BatchResult = pipeline_fn(local_paths, client)
        all_errors = download_errors + result.errors
        # Archive failures are tracked separately so a background-archive hiccup
        # does not turn the user-facing result card amber (item 8).
        archive_notes: list[str] = []

        # ------------------------------------------------------------------ #
        # 4b. Archive source documents (optional, defensive)
        # ------------------------------------------------------------------ #
        if archive is not None:
            for doc in result.docs:
                try:
                    if doc.path and doc.route is not None:
                        src = Path(doc.path)
                        if src.exists():
                            data = src.read_bytes()
                            archive.archive_source(
                                client.client_id,
                                doc.route.fy,
                                doc.route.bucket,
                                src.name,
                                data,
                            )
                except Exception as exc:  # noqa: BLE001
                    archive_notes.append(f"archive source {getattr(doc, 'path', '?')}: {exc}")

        # ------------------------------------------------------------------ #
        # 5. Upload workbooks + archive workbooks (optional, defensive)
        # ------------------------------------------------------------------ #
        uploaded: list[str] = []
        for filename, data in result.workbooks.items():
            try:
                upload_fn(channel_id, filename, data, filename)
                uploaded.append(filename)
            except Exception as exc:  # noqa: BLE001
                all_errors.append(f"{filename}: upload failed — {exc}")

            if archive is not None:
                try:
                    from app.archive import _fy_from_workbook_name
                    fy = _fy_from_workbook_name(filename)
                    if fy is not None:
                        archive.save_workbook(client.client_id, fy, filename, data)
                except Exception as exc:  # noqa: BLE001
                    archive_notes.append(f"archive workbook {filename}: {exc}")

        # ------------------------------------------------------------------ #
        # 6. Post result card
        # ------------------------------------------------------------------ #
        n_processed = sum(
            1 for doc in result.docs if not doc.note.startswith("ERROR")
        )
        coa_missing = client.status != "active"

        # Card: real failures drive the warning header; archive hiccups are a
        # muted context line only (item 8).
        say_fn(
            blocks=result_card(
                n_files=len(file_ids),
                n_processed=n_processed,
                workbooks=uploaded,
                errors=all_errors,
                coa_missing=coa_missing,
                archive_notes=archive_notes,
            ),
            text=f"Processed {n_processed}/{len(file_ids)} documents.",
        )

        # Outcome keeps archive failures in ``errors`` for observability/back-compat.
        return ShareOutcome(
            channel_id=channel_id,
            n_files=len(file_ids),
            workbooks=uploaded,
            n_processed=n_processed,
            errors=all_errors + archive_notes,
            status="ok",
        )

    except Exception as exc:  # noqa: BLE001
        return ShareOutcome(
            channel_id=channel_id,
            n_files=len(file_ids),
            workbooks=[],
            n_processed=0,
            errors=[str(exc)],
            status="error",
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
