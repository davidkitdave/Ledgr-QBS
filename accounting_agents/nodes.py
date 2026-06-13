"""Pure processing ``@node`` wrappers around the invoice_processing brain.

These nodes form the deterministic spine of the ADK 2.0 Document Workflow graph.
Each one recovers the uploaded PDF from the ADK artifact service, calls the
matching ``invoice_processing`` function(s), and writes results into ``ctx.state``
(and/or emits an ``Event`` with a ``route``).

Design rules (mirroring the pipeline's dependency-injection style):
- No accounting_agents-internal model literals leak in; the model is passed
  explicitly via ``MODEL_LITE`` / ``MODEL_STD`` so tests can inject their own.
- Every brain callable is injectable via a node-module-level seam (the ``*_FN``
  defaults) so unit tests can swap in fake callables without touching Gemini.
- Nodes never import accounting_agents heavy deps at call time beyond config.

State-key / artifact-filename convention (hand this to the Slack + graph tasks):
- The uploaded PDF is saved as an ADK artifact by the Slack layer under the
  filename ``inbox/{file_id}.pdf`` (see :data:`ARTIFACT_NAME_FMT`).
- The exact artifact filename for the current run is passed to the workflow via
  the state key :data:`ARTIFACT_NAME_KEY` = ``"temp:artifact_name"``.
- ``classify_node`` emits ``Event(route="invoice"|"bank_statement")`` and writes
  the resolved ``doc_type`` / ``direction`` back into state under
  :data:`DOC_TYPE_KEY` / :data:`DIRECTION_KEY`.
- ``extract_invoice_node`` writes a fan-out LIST of normalized invoices (as
  dicts) under :data:`NORMALIZED_KEY`; ``categorize_node`` / ``tax_node`` read
  and rewrite that same list.
- ``extract_bank_node`` writes a list of bank statements under
  :data:`BANK_STATEMENTS_KEY`.
- ``route_node`` writes per-document FY/sheet routing metadata under
  :data:`ROUTES_KEY`.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Literal, Optional

from google.adk.events import RequestInput
from google.adk.events.event import Event
from pydantic import BaseModel, Field

from invoice_processing.classify.document_classifier import (
    ClassificationResult,
    classify_document,
    resolve_direction,
)
from invoice_processing.export.categorizer import categorize_invoice
from invoice_processing.export.client_context import (
    category_mapping_from_state,
    coa_from_state,
    entity_memory_from_state,
)
from invoice_processing.export.exporters import (
    get_bank_exporter,
    get_exporter,
)
from invoice_processing.export.models import BankStatement, NormalizedInvoice
from invoice_processing.export.routing import DocRoute, route_document
from invoice_processing.export.tax_classifier import TaxClassifier
from invoice_processing.extract.bank_statement_extractor import (
    extract_bank_statement,
    to_bank_statements,
)
from invoice_processing.extract.invoice_extractor import (
    ExtractedInvoiceBundle,
    extract_invoice_bundle,
    reconcile,
    to_normalized,
)

from .config import MODEL_LITE, MODEL_STD

# --------------------------------------------------------------------------- #
# State-key + artifact-filename convention (shared with Slack + graph tasks)
# --------------------------------------------------------------------------- #

#: State key carrying the ADK artifact filename of the uploaded PDF for this run.
ARTIFACT_NAME_KEY = "temp:artifact_name"

#: Filename convention the Slack layer uses when it ``save_artifact``s the PDF.
ARTIFACT_NAME_FMT = "inbox/{file_id}.pdf"

#: State keys for routing / extraction outputs.
DOC_TYPE_KEY = "doc_type"
DIRECTION_KEY = "direction"
NORMALIZED_KEY = "normalized_invoices"
BANK_STATEMENTS_KEY = "bank_statements"
ROUTES_KEY = "doc_routes"

#: State key carrying the serializable ledger payload that ``consolidate_node``
#: prepares and the (Slack-owning) runner persists via ``SlackLedgerStore``.
LEDGER_ROWS_KEY = "ledger_rows"

#: State key carrying the final user-facing delivery summary string.
DELIVER_SUMMARY_KEY = "deliver_summary"

#: Routing labels emitted by classify_node.
ROUTE_INVOICE = "invoice"
ROUTE_BANK = "bank_statement"

#: Lines with a tax confidence strictly below this trigger HITL approval.
APPROVAL_CONFIDENCE_THRESHOLD = 0.7

#: State key recording the gate's outcome ("auto_approved" | the human decision).
APPROVAL_STATUS_KEY = "approval_status"


class ApproveDecision(BaseModel):
    """The human accountant's response to an approval request.

    Fed back into the paused workflow as the ``RequestInput`` response. ``edit``
    carries optional line-level corrections the downstream nodes may apply.
    """

    decision: Literal["approve", "edit", "reject"] = Field(
        default="approve",
        description="approve = post as-is; edit = post with the supplied edits; "
        "reject = drop the document.",
    )
    edits: Optional[dict[str, Any]] = Field(
        default=None,
        description="Optional structured corrections to apply when decision=='edit'.",
    )

# --------------------------------------------------------------------------- #
# Injectable brain seams (tests override these module attributes)
# --------------------------------------------------------------------------- #

CLASSIFY_FN: Callable[..., ClassificationResult] = classify_document
DIRECTION_FN: Callable[..., str] = resolve_direction
EXTRACT_BUNDLE_FN: Callable[..., ExtractedInvoiceBundle] = extract_invoice_bundle
EXTRACT_BANK_FN: Callable[..., Any] = extract_bank_statement
CATEGORIZE_FN: Callable[..., NormalizedInvoice] = categorize_invoice

# --------------------------------------------------------------------------- #
# Artifact recovery
# --------------------------------------------------------------------------- #


async def _load_pdf_bytes(ctx) -> tuple[bytes, str]:
    """Recover the uploaded PDF bytes + mime type from the ADK artifact service.

    Reads the artifact filename from ``ctx.state[ARTIFACT_NAME_KEY]`` and loads
    it via ``ctx.load_artifact``. Returns ``(data, mime_type)``.
    """
    filename = ctx.state.get(ARTIFACT_NAME_KEY)
    if not filename:
        raise ValueError(
            f"No artifact filename in state[{ARTIFACT_NAME_KEY!r}] — the Slack "
            "layer must save the PDF and set this key before the workflow runs."
        )
    part = await ctx.load_artifact(filename)
    if part is None or part.inline_data is None or part.inline_data.data is None:
        raise ValueError(f"Artifact {filename!r} is missing or has no inline bytes.")
    mime_type = part.inline_data.mime_type or "application/pdf"
    return part.inline_data.data, mime_type


def _parse_iso(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    from datetime import datetime

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _effective_fye_month(state: dict) -> tuple[int, bool]:
    """Return (fye_month, defaulted). Defaults to 12 (calendar year) when absent."""
    fye = state.get("fye_month")
    if fye is not None:
        return int(fye), False
    return 12, True


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #


async def classify_node(ctx) -> Event:
    """Classify the uploaded PDF and route to the invoice or bank-statement lane.

    Emits ``Event(route="invoice"|"bank_statement")`` and records the resolved
    ``doc_type`` + ``direction`` in state for downstream nodes.
    """
    data, mime_type = await _load_pdf_bytes(ctx)
    cls: ClassificationResult = CLASSIFY_FN(data, mime_type, model=MODEL_LITE)
    doc_type = (cls.doc_type or "other").strip().lower()

    if doc_type == "bank_statement":
        ctx.state[DOC_TYPE_KEY] = "bank_statement"
        ctx.state[DIRECTION_KEY] = None
        return Event(route=ROUTE_BANK, output={"doc_type": "bank_statement"})

    direction = DIRECTION_FN(cls, client_name=ctx.state.get("client_name"))
    ctx.state[DOC_TYPE_KEY] = doc_type
    ctx.state[DIRECTION_KEY] = direction
    return Event(
        route=ROUTE_INVOICE,
        output={"doc_type": doc_type, "direction": direction},
    )


async def extract_invoice_node(ctx) -> Event:
    """Extract + reconcile + normalize a bundle of invoices/receipts (fan-out).

    Calls ``extract_invoice_bundle`` with MODEL_LITE, then for EACH invoice in the
    bundle runs ``reconcile`` + ``to_normalized``, producing a LIST of normalized
    invoices written to ``state[NORMALIZED_KEY]``.
    """
    data, mime_type = await _load_pdf_bytes(ctx)
    bundle: ExtractedInvoiceBundle = EXTRACT_BUNDLE_FN(data, mime_type, model=MODEL_LITE)

    direction = ctx.state.get(DIRECTION_KEY)
    effective_direction = direction if direction in ("purchase", "sales") else "purchase"
    our_gst = bool(ctx.state.get("tax_registered", True))

    normalized: list[NormalizedInvoice] = []
    for ex in bundle.invoices:
        ok, _detail = reconcile(ex)
        inv = to_normalized(
            ex,
            direction=effective_direction,
            our_gst_registered=our_gst,
        )
        inv.reconciled = ok
        normalized.append(inv)

    ctx.state[NORMALIZED_KEY] = [_inv_to_dict(i) for i in normalized]
    return Event(output={"count": len(normalized)})


async def categorize_node(ctx) -> Event:
    """Fill COA account codes per normalized invoice (COA from client profile)."""
    invoices = _normalized_from_state(ctx.state)
    coa = coa_from_state(ctx.state)
    cat_map = category_mapping_from_state(ctx.state)
    ent_mem = entity_memory_from_state(ctx.state)

    for inv in invoices:
        CATEGORIZE_FN(
            inv,
            coa=coa,
            category_mapping=cat_map,
            entity_memory=ent_mem,
            model=MODEL_LITE,
        )

    ctx.state[NORMALIZED_KEY] = [_inv_to_dict(i) for i in invoices]
    return Event(output={"count": len(invoices)})


async def tax_node(ctx) -> Event:
    """Classify SG GST treatment per line, per normalized invoice."""
    invoices = _normalized_from_state(ctx.state)
    clf = TaxClassifier()
    for inv in invoices:
        for line in inv.lines:
            clf.classify_line(line, inv)

    ctx.state[NORMALIZED_KEY] = [_inv_to_dict(i) for i in invoices]
    return Event(output={"count": len(invoices)})


async def extract_bank_node(ctx) -> Event:
    """Extract a bank statement (MODEL_STD) into a list of BankStatements."""
    data, mime_type = await _load_pdf_bytes(ctx)
    result = EXTRACT_BANK_FN(data, mime_type, model=MODEL_STD)
    # extract_bank_statement returns (ExtractedBankStatement, mode_used); fakes
    # may return just the statement.
    if isinstance(result, tuple):
        ex_bank, mode_used = result
    else:
        ex_bank, mode_used = result, None

    statements = to_bank_statements(ex_bank, mode_used=mode_used)
    ctx.state[BANK_STATEMENTS_KEY] = [_bank_to_dict(s) for s in statements]
    return Event(output={"count": len(statements)})


async def route_node(ctx) -> Event:
    """Compute FY + sheet/direction routing metadata (NO GCS)."""
    fye_month, _defaulted = _effective_fye_month(ctx.state)
    client_id = ctx.state.get("client_id") or "unknown"
    doc_type = (ctx.state.get(DOC_TYPE_KEY) or "").strip().lower()
    direction = ctx.state.get(DIRECTION_KEY)

    routes: list[dict] = []

    if doc_type == "bank_statement":
        for s in _bank_from_state(ctx.state):
            # Bank statements route by representative date; derive it from the
            # stored BankStatement dict's transactions.
            rep_date = _bank_dict_representative_date(s)
            routes.append(
                _route_to_dict(
                    route_document(
                        doc_type="bank_statement",
                        direction=None,
                        doc_date=rep_date,
                        fye_month=fye_month,
                        client_id=client_id,
                        filename=ctx.state.get("source_filename", "statement.pdf"),
                    )
                )
            )
    else:
        for inv in _normalized_from_state(ctx.state):
            doc_date = inv.invoice_date or date.today()
            routes.append(
                _route_to_dict(
                    route_document(
                        doc_type=doc_type or "invoice",
                        direction=direction,
                        doc_date=doc_date,
                        fye_month=fye_month,
                        client_id=client_id,
                        filename=ctx.state.get("source_filename", "invoice.pdf"),
                    )
                )
            )

    ctx.state[ROUTES_KEY] = routes
    return Event(output={"count": len(routes)})


# --------------------------------------------------------------------------- #
# HITL + delivery nodes — PASS-THROUGH PLACEHOLDERS (owned by later tasks).
#
# The real logic lives in:
#   - approval_gate     -> task6 (HITL: yield RequestInput + Firestore interrupt)
#   - consolidate_node  -> task8 (SlackLedgerStore append rows to FY workbook)
#   - deliver_node      -> task8 (re-upload workbook to Slack + Firestore pointer)
#
# Until those tasks land, these are deterministic pass-throughs so the graph is
# importable and a wiring test can run a full document pass end-to-end with fakes.
# Each forwards state untouched and emits a small Event; none raise.
# --------------------------------------------------------------------------- #


def _needs_review(state: dict) -> tuple[bool, list[str]]:
    """Return ``(needs_review, reasons)`` by inspecting normalized invoices.

    A document needs human approval when ANY invoice/line is flagged, not
    reconciled, or carries a tax confidence below
    :data:`APPROVAL_CONFIDENCE_THRESHOLD`. Reasons are human-readable strings
    used to build the approval prompt.
    """
    reasons: list[str] = []
    for idx, inv in enumerate(_normalized_from_state(state)):
        label = inv.invoice_number or f"invoice #{idx + 1}"
        if not inv.reconciled:
            note = inv.reconcile_note or "totals do not reconcile"
            reasons.append(f"{label}: not reconciled ({note})")
        for line in inv.lines:
            if line.tax_flagged:
                reasons.append(
                    f"{label}: line '{line.description}' flagged for tax review"
                    + (f" ({line.tax_reason})" if line.tax_reason else "")
                )
            elif (
                line.tax_confidence is not None
                and line.tax_confidence < APPROVAL_CONFIDENCE_THRESHOLD
            ):
                reasons.append(
                    f"{label}: line '{line.description}' low tax confidence "
                    f"({line.tax_confidence:.2f} < {APPROVAL_CONFIDENCE_THRESHOLD})"
                )
    return (bool(reasons), reasons)


def _approval_interrupt_id(state: dict) -> str:
    """Stable interrupt id correlating the pause with the Slack drop.

    Prefers an explicit ``op_id`` set by the Slack layer; otherwise derives one
    from ``channel_id`` + ``file_id`` (channel = client = session scope).
    """
    op_id = state.get("op_id")
    if op_id:
        return str(op_id)
    channel = state.get("channel_id") or "unknown"
    file_id = state.get("file_id") or "unknown"
    return f"{channel}:{file_id}"


def _approval_summary(reasons: list[str]) -> str:
    """Human-readable summary of why the document needs approval."""
    header = (
        "Please review the proposed accounting entries — the following need a "
        "human decision before they are added to the ledger:"
    )
    bullets = "\n".join(f"  • {r}" for r in reasons)
    return f"{header}\n{bullets}"


async def approval_gate(ctx):
    """HITL gate: pause for human approval when any entry is uncertain.

    Inspects ``state[NORMALIZED_KEY]``. If any invoice/line is flagged, not
    reconciled, or below the confidence threshold, it ``yield``s a
    ``RequestInput`` to pause the resumable workflow (the Slack layer surfaces
    Approve / Edit / Reject buttons and, on the human's click, resumes the
    runner with an :class:`ApproveDecision`). Otherwise it auto-approves and the
    workflow proceeds without pausing.

    NOTE: this is an async generator (it ``yield``s) so a ``RequestInput`` is
    passed through as an interrupt. It must NOT be wrapped in a broad
    ``except`` — that would trap the framework's interrupt handling and break
    HITL resume.
    """
    needs_review, reasons = _needs_review(ctx.state)
    if not needs_review:
        ctx.state[APPROVAL_STATUS_KEY] = "auto_approved"
        return

    summary = _approval_summary(reasons)
    # Stash the summary in state so the (Slack-owning) runner can render the
    # approval card text without re-deriving it; the node stays Slack-agnostic.
    ctx.state["approval_message"] = summary
    yield RequestInput(
        interrupt_id=_approval_interrupt_id(ctx.state),
        message=summary,
        response_schema=ApproveDecision,
    )


def _doc_key(state: dict, sheet: str, identity: str, index: int) -> str:
    """Stable per-document dedupe key (re-processing re-emits the same key).

    Built from the Slack file id (the document's stable identity within a
    channel) + the destination sheet + the document's own identity (invoice
    number / account number) and its index in the run, so the same drop never
    double-appends but two genuinely different documents never collide.
    """
    file_id = state.get("file_id") or state.get("source_filename") or "doc"
    ident = (identity or "").strip() or f"i{index}"
    return f"{file_id}:{sheet}:{ident}"


async def consolidate_node(ctx) -> Event:
    """Gather this run's rows into a serializable ``state[LEDGER_ROWS_KEY]`` payload.

    SLACK-AGNOSTIC: this node performs NO Slack I/O. It uses the per-run routes
    (``route_node``) + the normalized invoices / bank statements to build export
    rows (via the same ``exporters.py`` the workbook writers use) and a stable
    per-document dedupe key, then stows them in ``state`` for the runner layer.
    The runner's ``SlackLedgerStore`` is what fetches/append/re-uploads the
    channel workbook — keeping all Slack ownership out of the graph.
    """
    state = ctx.state
    routes = state.get(ROUTES_KEY) or []
    doc_type = (state.get(DOC_TYPE_KEY) or "").strip().lower()
    client_id = state.get("client_id") or "unknown"
    software = state.get("software") or "qbs"

    # Representative FY for the run (the workbook is per-FY); take the first route.
    fy = str(routes[0]["fy"]) if routes else "unknown"

    batches: list[dict] = []

    if doc_type == "bank_statement":
        kind = "bank"
        exporter = get_bank_exporter()
        statements = _bank_statements_from_state(state)
        for idx, (stmt, route) in enumerate(zip(statements, routes)):
            sheet = stmt.bank_name or stmt.account_number or f"Account {idx + 1}"
            rows = exporter.bank_rows(stmt)
            batches.append(
                {
                    "sheet": sheet,
                    "doc_key": _doc_key(state, sheet, stmt.account_number or stmt.bank_name, idx),
                    "rows": rows,
                }
            )
    else:
        kind = "invoice"
        exporter = get_exporter(software)
        invoices = _normalized_from_state(state)
        for idx, (inv, route) in enumerate(zip(invoices, routes)):
            sheet = route.get("sheet") or "Purchase"
            row_doc_type = "sales" if sheet == "Sales" else "purchase"
            rows = exporter.rows([inv], row_doc_type)
            batches.append(
                {
                    "sheet": sheet,
                    "doc_key": _doc_key(state, sheet, inv.invoice_number, idx),
                    "rows": rows,
                }
            )

    payload = {
        "client_id": client_id,
        "fy": fy,
        "kind": kind,
        "software": software,
        "batches": batches,
    }
    state[LEDGER_ROWS_KEY] = payload
    state["consolidated_count"] = len(batches)
    return Event(output={"consolidated": len(batches), "fy": fy, "kind": kind})


def _month_label(rows: list[dict]) -> str:
    """Derive a human-readable month label from transaction date rows.

    Parses the ``Date`` field (DD/MM/YYYY) from each row and returns a compact
    label: a single month name for single-month statements, a range like
    "Jan–Mar 2025" for multi-month, or "" when no dates are parseable.
    """
    months: list[tuple[int, int]] = []  # (year, month) pairs
    for row in rows:
        d = _parse_iso(row.get("Date") or "")
        if d is not None:
            months.append((d.year, d.month))
    if not months:
        return ""
    unique = sorted(set(months))
    _MONTH_ABBR = [
        "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]
    if len(unique) == 1:
        yr, mo = unique[0]
        return f"{_MONTH_ABBR[mo]} {yr}"
    first_yr, first_mo = unique[0]
    last_yr, last_mo = unique[-1]
    if first_yr == last_yr:
        return f"{_MONTH_ABBR[first_mo]}–{_MONTH_ABBR[last_mo]} {first_yr}"
    return f"{_MONTH_ABBR[first_mo]} {first_yr}–{_MONTH_ABBR[last_mo]} {last_yr}"


def _closing_balance_from_rows(rows: list[dict]) -> Optional[float]:
    """Return the closing balance from a set of bank rows.

    Looks for the last TOTALS row's ``Stated Balance`` (the closing balance the
    exporter writes there), falling back to the last non-None ``Stated Balance``
    value found in any row.
    """
    last_stated: Optional[float] = None
    for row in rows:
        desc = row.get("Description") or ""
        stated = row.get("Stated Balance")
        if stated is not None:
            try:
                val = float(stated)
                if desc == "TOTALS":
                    return val
                last_stated = val
            except (TypeError, ValueError):
                pass
    return last_stated


async def deliver_node(ctx) -> Event:
    """Emit the final user-facing summary text (NO Slack I/O).

    SLACK-AGNOSTIC: ``deliver_node`` only composes the human-readable summary of
    what was consolidated and writes it to ``state[DELIVER_SUMMARY_KEY]``. The
    runner reads ``state[LEDGER_ROWS_KEY]`` to persist the workbook to Slack and
    posts this summary; the node itself never touches the Slack client.

    Bank summary format:
        "📒 Added **April 2025** (12 transactions) to your FY2025 ledger
         — closing balance SGD 4,210.55."
    Invoice summary format:
        "📒 Added **INV-001** (3 lines) to your FY2025 ledger."
    """
    state = ctx.state
    payload = state.get(LEDGER_ROWS_KEY) or {}
    batches = payload.get("batches") or []
    fy = payload.get("fy", "?")
    kind = payload.get("kind", "document")

    if not batches:
        state[DELIVER_SUMMARY_KEY] = "No ledger entries were produced for this document."
        state["delivered"] = True
        return Event(output={"delivered": True, "summary": state[DELIVER_SUMMARY_KEY]})

    if kind == "bank":
        # Build a named summary per account batch.
        parts: list[str] = []
        for batch in batches:
            rows = batch.get("rows") or []
            # Count only real transaction rows (exclude BALANCE B/F + TOTALS markers).
            txn_rows = [
                r for r in rows
                if (r.get("Description") or "") not in ("BALANCE B/F", "TOTALS")
            ]
            txn_count = len(txn_rows)
            month_label = _month_label(txn_rows)
            closing = _closing_balance_from_rows(rows)

            # Derive currency from the first row that has it.
            currency = next(
                (r.get("Currency") for r in rows if r.get("Currency")), "SGD"
            )

            label = f"**{month_label}**" if month_label else "statement"
            count_str = f"{txn_count} transaction{'s' if txn_count != 1 else ''}"
            bal_str = (
                f" — closing balance {currency} {closing:,.2f}"
                if closing is not None
                else ""
            )
            parts.append(f"{label} ({count_str}){bal_str}")

        summary = f"📒 Added {'; '.join(parts)} to your FY{fy} ledger."
    else:
        n_rows = sum(len(b.get("rows") or []) for b in batches)
        summary = (
            f"📒 Added {n_rows} line{'s' if n_rows != 1 else ''} from "
            f"{len(batches)} document{'s' if len(batches) != 1 else ''} "
            f"to your FY{fy} ledger."
        )

    state[DELIVER_SUMMARY_KEY] = summary
    state["delivered"] = True
    return Event(output={"delivered": True, "summary": summary})


# --------------------------------------------------------------------------- #
# Serialization helpers — NormalizedInvoice / BankStatement <-> plain dict
# (workflow state must be JSON-serializable basic types)
# --------------------------------------------------------------------------- #
from dataclasses import asdict, is_dataclass  # noqa: E402


def _inv_to_dict(inv: NormalizedInvoice) -> dict:
    d = asdict(inv)
    d["invoice_date"] = inv.invoice_date.isoformat() if inv.invoice_date else None
    d["due_date"] = inv.due_date.isoformat() if inv.due_date else None
    return d


def _dict_to_inv(d: dict) -> NormalizedInvoice:
    from invoice_processing.export.models import InvoiceLine, PartyInfo

    sup = PartyInfo(**(d.get("supplier") or {}))
    cus = PartyInfo(**(d.get("customer") or {}))
    lines = [InvoiceLine(**ld) for ld in (d.get("lines") or [])]
    return NormalizedInvoice(
        doc_type=d.get("doc_type", "purchase"),
        invoice_number=d.get("invoice_number"),
        invoice_date=_parse_iso(d.get("invoice_date")),
        due_date=_parse_iso(d.get("due_date")),
        currency=d.get("currency", "SGD"),
        po_number=d.get("po_number"),
        supplier=sup,
        customer=cus,
        lines=lines,
        our_gst_registered=bool(d.get("our_gst_registered", True)),
        reconciled=bool(d.get("reconciled", True)),
        reconcile_note=d.get("reconcile_note"),
    )


def _normalized_from_state(state: dict) -> list[NormalizedInvoice]:
    return [_dict_to_inv(d) for d in (state.get(NORMALIZED_KEY) or [])]


def _bank_to_dict(s: BankStatement) -> dict:
    d = asdict(s)
    d["transactions"] = [
        {**asdict(t), "date": t.date.isoformat() if t.date else None}
        for t in s.transactions
    ]
    return d


def _bank_from_state(state: dict) -> list[dict]:
    return list(state.get(BANK_STATEMENTS_KEY) or [])


def _dict_to_bank(d: dict) -> BankStatement:
    from dataclasses import fields as _dc_fields

    from invoice_processing.export.models import BankTransaction

    txn_fields = {f.name for f in _dc_fields(BankTransaction)}
    txns = []
    for t in d.get("transactions") or []:
        td = {k: v for k, v in t.items() if k in txn_fields}
        td["date"] = _parse_iso(td.get("date"))
        txns.append(BankTransaction(**td))
    bank_fields = {f.name for f in _dc_fields(BankStatement)} - {"transactions"}
    fields = {k: v for k, v in d.items() if k in bank_fields}
    return BankStatement(transactions=txns, **fields)


def _bank_statements_from_state(state: dict) -> list[BankStatement]:
    return [_dict_to_bank(d) for d in (state.get(BANK_STATEMENTS_KEY) or [])]


def _bank_dict_representative_date(s: dict) -> date:
    best: Optional[date] = None
    for t in s.get("transactions") or []:
        d = _parse_iso(t.get("date"))
        if d is not None and (best is None or d > best):
            best = d
    return best if best is not None else date.today()


def _route_to_dict(r: DocRoute) -> dict:
    return {
        "fy": r.fy,
        "bucket": r.bucket,
        "workbook": r.workbook,
        "sheet": r.sheet,
        "archive_path": r.archive_path,
    }
