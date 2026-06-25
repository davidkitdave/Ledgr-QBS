from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from invoice_processing.classify.document_classifier import classify_file, resolve_direction
from invoice_processing.export.categorizer import categorize_invoice
from invoice_processing.export.client_context import (
    category_mapping_from_state,
    coa_from_state,
    coa_keys_from_state,
    entity_memory_from_state,
)
from invoice_processing.export.exporters import (
    get_bank_exporter,
    get_exporter,
    validate_required_fields,
)
from invoice_processing.export.models import NormalizedInvoice
from invoice_processing.export.routing import route_document
from invoice_processing.extract.bank_statement_extractor import (
    extract_bank_file,
    to_bank_statements,
)
from invoice_processing.extract.invoice_extractor import mime_for
from invoice_processing.extract.process_invoice_document import (
    process_invoice_document,
)
from invoice_processing.pipeline import (
    BatchResult as EngineBatchResult,
    ProcessedDoc,
    _bank_representative_date,
    _build_bank_workbook,
    _build_ledger_workbook,
    _effective_fye_month,
)


def process_batch_with_document_spine(
    paths: list[str | Path],
    client: Any,
    **inject: Any,
) -> EngineBatchResult:
    """Process documents for the clean ADK tool using the multi-document spine."""

    classify_fn = inject.get("classify_fn") or classify_file
    direction_fn = inject.get("direction_fn") or resolve_direction
    bank_fn = inject.get("bank_fn") or extract_bank_file
    categorize_fn = inject.get("categorize_fn") or categorize_invoice
    invoice_process_fn = inject.get("invoice_process_fn") or process_invoice_document

    docs: list[ProcessedDoc] = []
    errors: list[str] = []
    for path in paths:
        try:
            docs.extend(
                _process_one_path(
                    Path(path),
                    client,
                    classify_fn=classify_fn,
                    direction_fn=direction_fn,
                    bank_fn=bank_fn,
                    categorize_fn=categorize_fn,
                    invoice_process_fn=invoice_process_fn,
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{path}: ERROR: {exc}")

    return EngineBatchResult(
        workbooks=_build_workbooks(docs, client),
        docs=docs,
        errors=errors,
    )


def _process_one_path(
    path: Path,
    client: Any,
    *,
    classify_fn: Any,
    direction_fn: Any,
    bank_fn: Any,
    categorize_fn: Any,
    invoice_process_fn: Any,
) -> list[ProcessedDoc]:
    fye_month, fye_defaulted = _effective_fye_month(client)
    client_id = client.client_id or "unknown"
    classification = classify_fn(str(path))
    doc_type = (classification.doc_type or "other").strip().lower()

    if doc_type == "bank_statement":
        result = bank_fn(str(path))
        ex_bank = result[0] if isinstance(result, tuple) else result
        rep_date = _bank_representative_date(ex_bank)
        route = route_document(
            doc_type="bank_statement",
            direction=None,
            doc_date=rep_date,
            fye_month=fye_month,
            client_id=client_id,
            filename=path.name,
        )
        note = "ok" + (" (fye_month defaulted to 12)" if fye_defaulted else "")
        return [
            ProcessedDoc(
                path=str(path),
                doc_type="bank_statement",
                direction=None,
                normalized=None,
                bank=ex_bank,
                route=route,
                reconciled=True,
                note=note,
            )
        ]

    direction = direction_fn(
        classification,
        client_name=client.client_name,
        client_uen=client.client_uen,
    )
    effective_direction = direction if direction in ("purchase", "sales") else "purchase"
    result = invoice_process_fn(
        path.read_bytes(),
        mime_for(path),
        doc_type=doc_type,
        direction=effective_direction,
        our_gst_registered=client.tax_registered,
        base_currency=client.base_currency,
        client_name=client.client_name,
        client_uen=client.client_uen,
    )

    state = client.to_state()
    docs: list[ProcessedDoc] = []
    for index, normalized in enumerate(result.normalized, start=1):
        _categorize_and_validate(
            normalized,
            client=client,
            categorize_fn=categorize_fn,
            state=state,
            effective_direction=effective_direction,
        )
        route = _route_normalized(
            normalized,
            client=client,
            source_path=path,
            direction=direction,
            fye_month=fye_month,
            index=index,
        )
        note = normalized.reconcile_note or "ok"
        if fye_defaulted:
            note = f"{note}; fye_month defaulted to 12"
        docs.append(
            ProcessedDoc(
                path=str(path),
                doc_type=normalized.document_kind or doc_type,
                direction=direction,
                normalized=normalized,
                bank=None,
                route=route,
                reconciled=bool(normalized.reconciled),
                note=note,
            )
        )
    return docs


def _categorize_and_validate(
    normalized: NormalizedInvoice,
    *,
    client: Any,
    categorize_fn: Any,
    state: dict[str, Any],
    effective_direction: str,
) -> None:
    categorize_fn(
        normalized,
        coa=coa_from_state(state),
        category_mapping=category_mapping_from_state(state),
        entity_memory=entity_memory_from_state(state),
    )
    exporter = get_exporter(client.accounting_software)
    exporter.configure_client_context(coa_keys=coa_keys_from_state(state))
    missing = validate_required_fields(normalized, exporter, effective_direction)
    if missing:
        normalized.reconciled = False
        review_note = "needs review: missing " + ", ".join(missing)
        normalized.reconcile_note = (
            f"{normalized.reconcile_note}; {review_note}"
            if normalized.reconcile_note
            else review_note
        )


def _route_normalized(
    normalized: NormalizedInvoice,
    *,
    client: Any,
    source_path: Path,
    direction: str | None,
    fye_month: int,
    index: int,
):
    filename = source_path.name
    if normalized.invoice_number:
        filename = f"{source_path.stem}-{normalized.invoice_number}{source_path.suffix}"
    elif index > 1:
        filename = f"{source_path.stem}-{index}{source_path.suffix}"
    return route_document(
        doc_type=normalized.document_kind or "invoice",
        direction=direction,
        doc_date=normalized.invoice_date or date.today(),
        fye_month=fye_month,
        client_id=client.client_id or "unknown",
        filename=filename,
    )


def _build_workbooks(docs: list[ProcessedDoc], client: Any) -> dict[str, bytes]:
    ledger_groups: dict[str, dict[str, list[NormalizedInvoice]]] = {}
    bank_groups: dict[str, list[Any]] = {}

    for doc in docs:
        if doc.note.startswith("ERROR") or (doc.normalized is None and doc.bank is None):
            continue
        workbook = doc.route.workbook
        if doc.doc_type == "bank_statement" and doc.bank is not None:
            bank_groups.setdefault(workbook, []).append(doc.bank)
        elif doc.normalized is not None:
            sheet = doc.route.sheet or "Purchase"
            ledger_groups.setdefault(workbook, {"Purchase": [], "Sales": []})
            ledger_groups[workbook].setdefault(sheet, []).append(doc.normalized)

    workbooks: dict[str, bytes] = {}
    exporter = get_exporter(client.accounting_software)
    exporter.configure_client_context(coa_keys=coa_keys_from_state(client.to_state()))
    for workbook, sheets in ledger_groups.items():
        workbooks[workbook] = _build_ledger_workbook(
            exporter,
            sheets.get("Purchase", []),
            sheets.get("Sales", []),
        )

    bank_exporter = get_bank_exporter()
    for workbook, banks in bank_groups.items():
        statements = []
        for bank in banks:
            statements.extend(to_bank_statements(bank))
        if statements:
            workbooks[workbook] = _build_bank_workbook(bank_exporter, statements)

    return workbooks
