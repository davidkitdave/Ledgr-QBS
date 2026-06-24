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
- ``extract_invoice_document_node`` writes a fan-out LIST of normalized invoices (as
  dicts) under :data:`NORMALIZED_KEY`; ``categorize_node`` / ``tax_node`` read
  and rewrite that same list.
- ``extract_bank_node`` writes a list of bank statements under
  :data:`BANK_STATEMENTS_KEY`.
- ``route_node`` writes per-document FY/sheet routing metadata under
  :data:`ROUTES_KEY`.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Callable, Literal, Optional

from google.adk.events import RequestInput
from google.adk.events.event import Event
from google.adk.workflow import node
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
    coa_keys_from_state,
    entity_memory_from_state,
    tax_codes_from_state,
)
from invoice_processing.export.axis_resolvers import (
    resolve_currency,
    resolve_software,
    resolve_tax_classifier_reference,
)
from invoice_processing.export.exporters import (
    bank_sheet_title,
    collect_account_flagged_summary,
    collect_export_unmapped_summary,
    collect_import_readiness,
    format_account_flagged_note,
    format_import_readiness_note,
    format_unmapped_export_note,
    get_bank_exporter,
    get_exporter,
    software_label,
)
from invoice_processing.export.models import BankStatement, NormalizedInvoice
from invoice_processing.export.routing import DocRoute, route_document
from ledgr_agent.review.grouping import partition_and_group_reasons
# Jurisdiction + LLM tax reasoning (multi-country support, replaces the
# previous SG-only TaxClassifier call inside tax_node).
from .jurisdiction import (
    CUSTOMER_COUNTRY_KEY,
    FLAG_FOR_HUMAN_KEY,
    JURISDICTION_AMBIGUOUS,
    JURISDICTION_RATES_KEY,
    JURISDICTION_REVIEW_REASON_KEY,
    SUPPLIER_COUNTRY_KEY,
    TAX_JURISDICTION_KEY,
    _norm_region,
    _resolve_client_currency,
    resolution_from_state as _resolution_from_state,
    resolve_jurisdiction as _resolve_jurisdiction_fn,
    write_to_state as _write_jurisdiction_to_state,
)
from .observability.sentry_trends import (
    emit_account_flagged_from_state,
    emit_from_struggle_state,
)
from .tax_reasoning import reason_one_invoice as _reason_one_invoice

from .normalized_invoice_codec import (
    bank_to_dict,
    dict_to_bank,
    dict_to_invoice,
    invoice_to_dict,
)
from .ledger_doc_identity import ledger_doc_identity
from invoice_processing.extract.bank_statement_extractor import (
    extract_bank_statement,
    to_bank_statements,
)
from invoice_processing.extract.document_extractor import extract_document_bundle
from invoice_processing.extract.document_record import DocumentRecordBundle
from invoice_processing.extract.process_invoice_document import (
    InvoiceProcessResult,
    process_invoice_document,
)
from invoice_processing.extract.invoice_extractor import (
    ExtractedInvoiceBundle,
    _is_soa_summary_invoice,
    append_direction_review_note,
    direction_needs_review,
    extract_invoice_bundle,
    reconcile,
    to_normalized,
)

from .config import MODEL_LITE, MODEL_READ, MODEL_STD

logger = logging.getLogger(__name__)

#: ADK state-size guard thresholds. Per-doc sessions keep these payloads small;
#: these limits make unexpectedly large bundles visible instead of silently
#: bloating the session (ADK guidance: large data → artifacts, not state).
_MAX_STATE_ITEMS = 50
_MAX_STATE_PAYLOAD_BYTES = 262144  # 256 KB


def _guard_state_payload(key: str, items: list) -> list:
    """ADK state-size guard. Per-doc sessions keep these lists small; if a
    payload ever exceeds the count/size thresholds, WARN (ADK guidance:
    large data belongs in artifacts, not session state — see
    https://adk.dev/graphs/data-handling). We don't migrate here because the
    Slack/HITL layer reads this from state; the warning makes an unexpectedly
    large bundle visible instead of silently bloating the session. Returns
    `items` unchanged so callers can wrap their assignment."""
    if len(items) > _MAX_STATE_ITEMS:
        logger.warning(
            "ADK state-size guard: key=%r has %d items (threshold=%d) — "
            "consider offloading to artifacts for large payloads",
            key, len(items), _MAX_STATE_ITEMS,
        )
    try:
        size = len(json.dumps(items, default=str).encode())
        if size > _MAX_STATE_PAYLOAD_BYTES:
            logger.warning(
                "ADK state-size guard: key=%r serialized payload is %d bytes "
                "(threshold=%d) — consider offloading to artifacts",
                key, size, _MAX_STATE_PAYLOAD_BYTES,
            )
    except Exception:
        logger.debug(
            "ADK state-size guard: could not estimate serialized size for key=%r; skipping size check",
            key,
        )
    return items


# --------------------------------------------------------------------------- #
# State-key + artifact-filename convention (shared with Slack + graph tasks)
# --------------------------------------------------------------------------- #

#: State key carrying the ADK artifact filename of the uploaded PDF for this run.
ARTIFACT_NAME_KEY = "temp:artifact_name"

#: Filename convention the Slack layer uses when it ``save_artifact``s the PDF.
#:
#: ADK's FastAPI dev server registers artifact routes with a single ``{artifact_name}``
#: path parameter, which by default does NOT match the slash character. Names like
#: ``inbox/upload.pdf`` therefore return 404 in the dev UI even when the file is
#: on disk. To keep dev tooling working we collapse the path to a flat
#: ``"{file_id}.pdf"`` in non-prod; prod keeps the namespace prefix for collision
#: safety with other tools writing into the same artifact bucket.
ARTIFACT_NAME_FMT = "inbox/{file_id}.pdf"


def artifact_name_for(file_id: str) -> str:
    """Return the artifact filename to use for ``file_id`` in the current env.

    In **every** non-prod environment the flat form (``"{file_id}.pdf"``) is
    returned so the dev FastAPI route matches. ADK's dev FastAPI registers
    artifact routes with a single ``{artifact_name}`` path parameter that
    does NOT match the slash character — names like ``inbox/upload.pdf``
    therefore return 404 in the dev UI even when the file is on disk.

    Prod keeps the namespaced ``"inbox/{file_id}.pdf"`` form for collision
    safety alongside other artifacts.

    Previous behaviour gated the flat form on ``is_playground_seed_enabled()``
    which is enabled in dev/unset but ALSO active in any non-prod scenario
    where a playground seed was used. Phase 1 / artifact-dev-naming
    simplifies the gate to a direct ``LEDGR_ENV != "prod"`` check so the
    flat form is used universally outside prod — eliminates the 404 in any
    ADK web / agents-cli playground session regardless of seed state.
    """
    import os as _os
    from .config import is_playground_seed_enabled

    env = (_os.environ.get("LEDGR_ENV") or "dev").strip().lower()
    if env != "prod" or is_playground_seed_enabled():
        return f"{file_id}.pdf"
    return ARTIFACT_NAME_FMT.format(file_id=file_id)

#: State keys for routing / extraction outputs.
DOC_TYPE_KEY = "doc_type"
DIRECTION_KEY = "direction"
NORMALIZED_KEY = "normalized_invoices"
DOCUMENT_RECORDS_KEY = "document_records"
LEDGER_SUMMARY_TABLE_KEY = "ledger_summary_table"
EXTRACTION_PATH_KEY = "extraction_path"
BOOKING_PROPOSALS_KEY = "booking_proposals"
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

#: State key carrying classify_node's confidence (feeds the extract reviewer's
#: low-confidence signal — see ``detect_struggle``).
CLASSIFY_CONFIDENCE_KEY = "classify_confidence"

#: A classify confidence strictly below this trips the reviewer's
#: ``low_classify_confidence`` signal.
CLASSIFY_CONFIDENCE_FLOOR = 0.60

#: Extract-reviewer state keys + verdict vocabulary (mid-flow "smart inspector"
#: between extraction and categorization). The reviewer only spends an LLM call
#: when cheap deterministic signals say the reader struggled (``detect_struggle``);
#: the happy path writes ``REVIEW_VERDICT_OK`` and never calls ``REVIEWER_FN``.
REVIEW_VERDICT_KEY = "review_verdict"
REVIEW_REASON_KEY = "review_reason"
REVIEW_VERDICT_OK = "ok"
REVIEW_VERDICT_HINTS = "hints_needed"
REVIEW_VERDICT_CLARIFY = "user_clarify"

#: §9.3 ceiling — bounded IN-NODE loop (NOT a graph cycle): at most this many
#: reviewer calls and re-extracts before circuit-breaking to a human (§9.5).
REVIEW_MAX_REVIEWS = 2
REVIEW_MAX_REEXTRACTS = 1

#: WS4 — one totals-focused reconcile re-read before HITL when unreconciled is the
#: only tripped signal (``review_extraction_node`` + ``approval_gate``).
RECONCILE_REEXTRACT_ATTEMPTED_KEY = "reconcile_reextract_attempted"
RECONCILE_REREAD_HINT = (
    "Re-read the document focusing on invoice totals, subtotals, tax amounts, "
    "and line sums — ensure they reconcile."
)

#: Lever 2 (ADR-0017 §3) — soft-signal prefixes.
#: A reason whose string starts with one of these is classified as SOFT and may
#: be cleared by the LLM-as-judge critic without human escalation.
#:
#: FAIL-SAFE: any reason string that does NOT start with a known soft prefix is
#: treated as HARD and always escalates, regardless of what the critic says.
#: This means a future signal that is genuinely soft but whose prefix is not
#: listed here will conservatively escalate — which is the safe direction.
#: Add new soft prefixes here only after deliberate review.
SOFT_SIGNAL_PREFIXES: tuple[str, ...] = (
    "doc_type_other",
    "doc_type_unfamiliar",
    "low_classify_confidence",
    "direction_uncertain",
)

#: Lever 4 (ADR-0017 §6) — per-client familiarity gate.
#: State key holding the familiarity map ``{key: {seen_count: n, ...}}``
#: loaded by the profile callback (``make_load_client_by_channel_callback``).
FAMILIARITY_KEY = "familiarity"

#: Minimum seen_count at which a doc shape is considered familiar enough to
#: suppress soft-only signals without a critic call.  Set to 2 so the Engine
#: sees at least two clean approvals before trusting a shape unconditionally.
FAMILIARITY_THRESHOLD = 2

#: Lever 3 (ADR-0017 §2) — open-set / zero-shot classify state keys.
#: ``CLASSIFY_FREE_TYPE_KEY`` carries the model's best free-text label for the
#: document type when it does not match an ALLOWED_DOC_TYPES enum value (e.g.
#: "delivery_order", "purchase_order").  Populated by ``classify_node``; used
#: by ``compose_confident_note`` to produce a human-readable label on the
#: confident path.  None when the model returned a recognised enum type.
CLASSIFY_FREE_TYPE_KEY = "classify_free_type"

#: ``CLASSIFY_PROCESSABLE_KEY`` carries the classifier's verdict on whether the
#: document has any bookable financial content (True) or is genuinely unbookable
#: (False — e.g. a blank page, marketing flyer, non-financial legal contract).
#: When False, ``detect_struggle`` appends a ``"processable_false"`` HARD signal
#: that always escalates to a human — it is not suppressible by the critic
#: (Lever 2) or by familiarity (Lever 4).  Defaults to True (safe: absent key
#: = processable).
CLASSIFY_PROCESSABLE_KEY = "classify_processable"

#: Fields on an invoice line that the HITL Edit flow may overwrite. These MUST
#: match the canonical ``InvoiceLine`` model field names (``invoice_processing/
#: export/models.py``) — the exporter reads ``line.tax_treatment`` /
#: ``line.net_amount`` when writing the ledger row, so an edit applied to
#: ``tax_code`` / ``amount`` (the pre-2026-06-15 names) is a silent no-op on
#: the actual ledger column. See ADR-0008's §1.5a follow-up and memory
#: ``ledgr-live-qa-state-2026-06`` for the live-QA-caught bug.
EDITABLE_LINE_FIELDS: tuple[str, ...] = (
    "account_code", "tax_treatment", "net_amount", "description",
)


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


class ReviewClarifyDecision(BaseModel):
    """The human's response to a MID-FLOW extract-review clarification request.

    Fed back into the paused workflow as the ``review_extraction_node``'s
    ``RequestInput`` response when the deterministic reviewer circuit-breaks to a
    human (§9.5). Distinct from :class:`ApproveDecision` (the terminal gate):
    this one steers the *re-extraction*, the terminal gate steers *posting*.

    Canonical field names only (§0.5-D):
    * ``reextract_as`` — re-run extraction with ``hint`` appended.
    * ``confirm_as_is`` — wave the current extraction through unchanged.
    * ``reject`` — drop the document (empties the normalized payload).
    """

    action: Literal["reextract_as", "confirm_as_is", "reject"] = Field(
        default="confirm_as_is",
        description="reextract_as = re-extract with the supplied hint; "
        "confirm_as_is = accept the current extraction; reject = drop the document.",
    )
    hint: Optional[str] = Field(
        default=None,
        description="Free-text steering hint appended to the extraction prompt "
        "when action=='reextract_as'.",
    )

# --------------------------------------------------------------------------- #
# Injectable brain seams (tests override these module attributes)
# --------------------------------------------------------------------------- #

CLASSIFY_FN: Callable[..., ClassificationResult] = classify_document
DIRECTION_FN: Callable[..., str] = resolve_direction
EXTRACT_BUNDLE_FN: Callable[..., ExtractedInvoiceBundle] = extract_invoice_bundle
EXTRACT_DOCUMENT_FN: Callable[..., DocumentRecordBundle] = extract_document_bundle
EXTRACT_INVOICE_DOCUMENT_FN: Callable[..., InvoiceProcessResult] = process_invoice_document
EXTRACT_BANK_FN: Callable[..., Any] = extract_bank_statement
CATEGORIZE_FN: Callable[..., NormalizedInvoice] = categorize_invoice
#: The mid-flow extract critic. Defaults to a small Gemini reader on MODEL_LITE;
#: tests swap a fake critic so NO network is touched. Signature mirrors the other
#: seams: ``REVIEWER_FN(state, reasons, *, model) -> dict`` with a ``verdict`` key
#: (one of REVIEW_VERDICT_OK / _HINTS / _CLARIFY) plus optional ``hint`` /
#: ``question``. Assigned below once ``_reviewer_llm`` is defined.
REVIEWER_FN: Callable[..., dict]

# --------------------------------------------------------------------------- #
# Artifact recovery
# --------------------------------------------------------------------------- #


def _is_document_mime(mime: str) -> bool:
    """Return True for mime types accepted as document bytes."""
    return mime.startswith("image/") or mime in ("application/pdf", "application/octet-stream", "")


async def _load_pdf_bytes(ctx) -> tuple[bytes, str]:
    """Recover the uploaded PDF bytes + mime type from the ADK artifact service.

    Fallback chain (in order):

    1. **Slack path** — ``ctx.state[ARTIFACT_NAME_KEY]`` is set: load via
       ``ctx.load_artifact``.  Behaviour is identical to the original
       implementation, including the "missing or has no inline bytes" error.

    2. **Playground / adk-web path** — scan ``ctx.user_content.parts`` for the
       first Part whose ``inline_data.data`` is non-empty and whose mime is a
       PDF or image (or octet-stream / missing → treated as application/pdf).

    3. **list_artifacts fallback** — call ``ctx.list_artifacts()`` and pick the
       most suitable key (prefer one ending in ``.pdf`` or with an image mime);
       load via ``ctx.load_artifact``.

    4. **Nothing found** — raise ``ValueError`` listing what was tried.

    Paths 2 and 3 *heal the precondition*: the recovered bytes are saved as an
    ADK artifact and ``ctx.state[ARTIFACT_NAME_KEY]`` is set so that downstream
    nodes (which also call ``_load_pdf_bytes``) behave identically to the Slack
    path.  Path 1 is idempotent — no re-save occurs.
    """
    # ------------------------------------------------------------------ #
    # Path 1: Slack path — artifact key already in state
    # ------------------------------------------------------------------ #
    filename = ctx.state.get(ARTIFACT_NAME_KEY)
    if filename:
        part = await ctx.load_artifact(filename)
        if part is None or part.inline_data is None or part.inline_data.data is None:
            raise ValueError(f"Artifact {filename!r} is missing or has no inline bytes.")
        mime_type = part.inline_data.mime_type or "application/pdf"
        return part.inline_data.data, mime_type

    # ------------------------------------------------------------------ #
    # Path 2: inline_data in ctx.user_content.parts  (playground / adk-web)
    # ------------------------------------------------------------------ #
    user_content = getattr(ctx, "user_content", None)
    parts = getattr(user_content, "parts", None) if user_content is not None else None
    if parts:
        for p in parts:
            inline = getattr(p, "inline_data", None)
            if inline is None:
                continue
            data = getattr(inline, "data", None)
            if not data:  # None or empty bytes
                continue
            mime = getattr(inline, "mime_type", None) or ""
            if not _is_document_mime(mime):
                continue
            # Normalise octet-stream / missing to application/pdf
            if mime in ("application/octet-stream", ""):
                mime = "application/pdf"
            # Heal: persist so downstream nodes see the same state as Slack path
            heal_name = artifact_name_for("upload")
            from google.genai import types as _genai_types
            saved_part = _genai_types.Part(
                inline_data=_genai_types.Blob(data=data, mime_type=mime)
            )
            await ctx.save_artifact(heal_name, saved_part)
            ctx.state[ARTIFACT_NAME_KEY] = heal_name
            return data, mime

    # ------------------------------------------------------------------ #
    # Path 3: list_artifacts fallback
    # ------------------------------------------------------------------ #
    keys: list[str] = []
    try:
        keys = await ctx.list_artifacts() or []
    except Exception:
        pass

    if keys:
        # Prefer keys ending in .pdf or image extensions; fall back to first key
        def _key_score(k: str) -> int:
            k_lower = k.lower()
            if k_lower.endswith(".pdf"):
                return 2
            if any(k_lower.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".tiff", ".webp")):
                return 1
            return 0

        best_key = max(keys, key=_key_score)
        part = await ctx.load_artifact(best_key)
        if part is not None and part.inline_data is not None and part.inline_data.data:
            mime_type = part.inline_data.mime_type or "application/pdf"
            # Heal
            ctx.state[ARTIFACT_NAME_KEY] = best_key
            return part.inline_data.data, mime_type

    # ------------------------------------------------------------------ #
    # Path 4: nothing available
    # ------------------------------------------------------------------ #
    tried = (
        f"state[{ARTIFACT_NAME_KEY!r}] was absent, "
        f"user_content.parts had no usable inline_data, "
        f"list_artifacts returned {keys!r} with no loadable bytes"
    )
    raise ValueError(
        f"No PDF bytes could be recovered for this session. Tried: {tried}. "
        "If running via Slack, ensure the runner sets state[ARTIFACT_NAME_KEY] "
        "before the workflow starts. If running via adk web / playground, "
        "upload a PDF or image file with your message."
    )


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


@node
async def classify_node(ctx) -> Event:
    """Classify the uploaded PDF and route to the invoice or bank-statement lane.

    Emits ``Event(route="commercial_doc"|"bank_statement")`` (canonical lane
    label from :mod:`accounting_agents.lane_config`) and records the resolved
    ``doc_type`` in state for downstream nodes. ``classify_node`` and the
    document workflow driver BOTH consult :data:`lane_config.DOC_TYPE_TO_LANE`
    so the Event route label and the iterated node list never disagree —
    that was the "route: invoice vs doc_type: receipt" trace gap (Phase 2).

    Direction is intentionally NOT resolved here for the invoice lane — the
    Understand (Drive-parity) call now owns parties + ``direction_for_client``
    in one structured shot, replacing the legacy ``classify_document`` +
    ``resolve_direction`` two-step that drove the Contractor Beta misclassification.
    The Understand-extract caller (``_resolve_direction_from_extract``) fills
    ``DIRECTION_KEY`` after the extract runs; if the Understand call returns
    ``"unknown"`` the document is parked at the HITL gate (not silently
    rewritten by a fuzzy Python pass).
    """
    from invoice_processing.classify.document_classifier import ALLOWED_DOC_TYPES
    from .lane_config import ROUTE_BANK, ROUTE_COMMERCIAL_DOC

    # Playground seed: inject synthetic ClientContext when running under adk web /
    # agents-cli and no real Slack profile is present.  This is the WS3a fix —
    # the coordinator (which had the before_agent_callback) was removed in ADR-0021,
    # so the seed must happen at the first node instead.  Function-local import
    # avoids the circular import (agent.py imports nodes at module level).
    from accounting_agents.agent import seed_playground_profile_if_needed  # noqa: PLC0415
    seed_playground_profile_if_needed(ctx.state)

    data, mime_type = await _load_pdf_bytes(ctx)
    cls: ClassificationResult = CLASSIFY_FN(data, mime_type, model=MODEL_LITE)
    doc_type = (cls.doc_type or "other").strip().lower()

    # Lever 3 (ADR-0017 §2) — apply the off-enum clamp at the node level so it
    # fires even when tests inject a fake CLASSIFY_FN that bypasses the real
    # classify_document() function's own clamp.  The two clamps are idempotent.
    if doc_type not in ALLOWED_DOC_TYPES:
        if not cls.free_type:
            cls.free_type = doc_type
        doc_type = "other"
        cls.doc_type = "other"

    # Persist the classifier's confidence so the extract reviewer's
    # ``low_classify_confidence`` signal (#5) can read it cheaply downstream.
    ctx.state[CLASSIFY_CONFIDENCE_KEY] = cls.confidence

    # Lever 3 (ADR-0017 §2) — persist free_type and processable verdict.
    # free_type carries the model's best raw label when the type is off-enum
    # (e.g. "delivery_order"); None for recognised enum types.
    ctx.state[CLASSIFY_FREE_TYPE_KEY] = cls.free_type or None
    # processable=False means the document cannot be meaningfully booked at all
    # (blank page, marketing flyer, non-financial contract).  Default is True.
    ctx.state[CLASSIFY_PROCESSABLE_KEY] = cls.processable

    # Resolve route label via lane_config — single source of truth. We keep
    # ``state[DOC_TYPE_KEY]`` as the raw enum ("invoice" / "receipt" / ...) so
    # downstream nodes / traces show what the LLM classifier actually emitted;
    # the Event route is the canonical lane label.
    if doc_type == "bank_statement":
        ctx.state[DOC_TYPE_KEY] = "bank_statement"
        ctx.state[DIRECTION_KEY] = None
        return Event(route=ROUTE_BANK, output={"doc_type": "bank_statement"})

    ctx.state[DOC_TYPE_KEY] = doc_type
    # Invoice lane: leave direction unset; the Understand caller fills it in
    # based on ``direction_for_client``. Until then, the extract step passes
    # ``direction="auto"`` so the Understand verdict is honored. If the
    # extract ever reports ``"unknown"``, the HITL gate (review_extraction_node
    # or approval_gate) escalates — no fuzzy Python fallback in the graph.
    ctx.state[DIRECTION_KEY] = "auto"
    return Event(
        route=ROUTE_COMMERCIAL_DOC,
        output={"doc_type": doc_type, "direction": "auto"},
    )


def _resolve_direction_from_extract(
    extract: Optional[dict],
    fallback: str = "unknown",
) -> str:
    """Read ``direction_for_client`` from the Understand extract.

    Per the Batch Direction plan, the Understand call owns the direction
    decision. Returns the resolved direction string (``"purchase"`` /
    ``"sales"`` / ``"self_referential"``) or ``"unknown"`` when the extract is
    missing or the model returned ``"unknown"``. Never silently assumes
    ``"purchase"`` — callers escalate unknown/self_referential to HITL.
    """
    if not extract:
        return fallback
    documents = extract.get("documents")
    if isinstance(documents, list) and documents:
        direction = documents[0].get("direction_for_client")
    else:
        direction = extract.get("direction_for_client")
    if direction in ("purchase", "sales", "self_referential"):
        return direction
    if direction == "unknown":
        return "unknown"
    return fallback


def _apply_invoice_process_result(ctx, result: InvoiceProcessResult) -> None:
    """Write shared orchestrator output into session state."""
    ctx.state[EXTRACTION_PATH_KEY] = result.extraction_path
    ctx.state[LEDGER_SUMMARY_TABLE_KEY] = result.summary_table
    if result.ledger_extract is not None:
        ctx.state["ledger_extract"] = result.ledger_extract
    if result.document_records is not None:
        ctx.state[DOCUMENT_RECORDS_KEY] = _guard_state_payload(
            DOCUMENT_RECORDS_KEY, result.document_records
        )
    elif result.extraction_path == "understand":
        ctx.state[DOCUMENT_RECORDS_KEY] = _guard_state_payload(DOCUMENT_RECORDS_KEY, [])
    if result.skipped_pages is not None:
        ctx.state["skipped_pages"] = result.skipped_pages
    if result.input_page_count is not None:
        ctx.state["input_page_count"] = result.input_page_count
    if result.partial_failure_warnings:
        ctx.state["partial_failure_warnings"] = list(result.partial_failure_warnings)
    ctx.state["normalized_invoice_count"] = len(result.normalized)
    if result.document_read_notes:
        ctx.state["document_read_notes"] = result.document_read_notes
    if result.booking_proposals is not None:
        ctx.state[BOOKING_PROPOSALS_KEY] = _guard_state_payload(
            BOOKING_PROPOSALS_KEY, result.booking_proposals
        )
    file_id = (ctx.state.get("file_id") or "").strip()
    normalized = list(result.normalized)
    if file_id:
        for inv in normalized:
            if not inv.source_file_id:
                inv.source_file_id = file_id
    ctx.state[NORMALIZED_KEY] = _guard_state_payload(
        NORMALIZED_KEY, [_inv_to_dict(i) for i in normalized]
    )


def _base_currency_from_state(state: dict) -> str:
    """Registry-aware client currency for extract/normalize — no silent SGD default."""
    region = _norm_region(state.get("client_region") or state.get("region") or "")
    resolved = _resolve_client_currency(state, region)
    return resolved or ""


def _client_region_and_currency_from_state(state: dict) -> tuple[str, str]:
    region = _norm_region(state.get("client_region") or state.get("region") or "")
    currency = _resolve_client_currency(state, region) or ""
    return region, currency


def _resolve_software_from_state(state: dict) -> tuple[str, bool]:
    """Return (canonical software key, flagged). Blank key when unresolved."""
    res = resolve_software(state.get("software"))
    if res.flagged:
        state["software_unresolved"] = res.reason
        return "", True
    return res.value or "", False


def _our_gst_registered_from_state(state: dict) -> bool:
    """Map session tax_registered to extract bool — unknown (None) must not assume registered."""
    val = state.get("tax_registered")
    if val is None:
        return False
    return bool(val)


@node
async def extract_invoice_document_node(ctx) -> Event:
    """Understand extraction — single orchestrated invoice lane step."""
    data, mime_type = await _load_pdf_bytes(ctx)
    review_hint = (ctx.state.get("review_hint") or "").strip()
    result = EXTRACT_INVOICE_DOCUMENT_FN(
        data,
        mime_type,
        doc_type=ctx.state.get(DOC_TYPE_KEY) or "invoice",
        direction=ctx.state.get(DIRECTION_KEY) or "auto",
        our_gst_registered=_our_gst_registered_from_state(ctx.state),
        base_currency=_base_currency_from_state(ctx.state),
        client_name=ctx.state.get("client_name"),
        client_uen=ctx.state.get("client_uen"),
        hint=review_hint or None,
        model=MODEL_READ,
    )
    _apply_invoice_process_result(ctx, result)
    # Drive-parity direction: the Understand call owns the sales/purchase
    # decision via ``direction_for_client``. Read it from the extract and
    # write it back into DIRECTION_KEY so downstream nodes (consolidate,
    # tax classifier, exporters) see a single, consistent direction. If the
    # model returned ``"unknown"`` we leave the literal string in place so
    # the HITL gate (review_extraction_node / approval_gate) can surface the
    # ambiguity — never silently rewrite via fuzzy Python matching.
    if result.extraction_path == "understand" and result.ledger_extract:
        resolved = _resolve_direction_from_extract(
            result.ledger_extract,
            fallback=ctx.state.get(DIRECTION_KEY) or "unknown",
        )
        if resolved == "unknown":
            resolved = _retry_resolve_direction_llm(ctx, result.ledger_extract)
        ctx.state[DIRECTION_KEY] = resolved
    return Event(output={"count": len(result.normalized)})


def _is_playground_placeholder(name: object) -> bool:
    """True when ``name`` looks like the dev playground's synthetic client name.

    The playground seed (``load_client_profile`` → ``_playground_default_context``)
    uses one of a small set of recognisable placeholder strings. Treating them as
    "real" clients makes the direction classifier fail (no match on the
    document) and every test invoice resolve to ``unknown`` — which is the
    opposite of what the playground is for. When we see a placeholder, we tell
    the LLM to ignore the client name and reason from document context only.
    """
    if not isinstance(name, str):
        return False
    s = name.strip().lower()
    if not s:
        return True
    placeholders = {
        "playground client",
        "playground",
        "test client",
        "demo client",
        "default client",
    }
    return s in placeholders or s.startswith("playground ") or s.startswith("test ")


def _retry_resolve_direction_llm(ctx, extract: dict) -> str:
    """Refined retry logic to disambiguate direction using the LLM when first pass is unknown.

    Special-cased: if the seeded client name is a recognisable playground
    placeholder (so the user is running ``adk web`` / playground without a real
    profile), the client-name match logic is skipped and the LLM is told to
    infer the most likely direction from the document parties alone — purchase
    is the right default for a typical test invoice.
    """
    client_name = ctx.state.get("client_name")
    client_uen = ctx.state.get("client_uen")
    client_is_placeholder = _is_playground_placeholder(client_name)

    if not client_name:
        return "unknown"

    from_party = extract.get("from_party") or {}
    to_party = extract.get("to_party") or {}
    documents = extract.get("documents") or []
    if documents:
        first_doc = documents[0]
        issuer_name = first_doc.get("vendor")
        issuer_uen = first_doc.get("vendor_tax_regno")
        bill_to_name = first_doc.get("buyer")
        bill_to_uen = None
        doc_kind = first_doc.get("doc_type") or "invoice"
        summary_str = (
            f"- reference: {first_doc.get('reference')}\n"
            f"- vendor: {issuer_name or 'Not visible'}\n"
            f"- buyer: {bill_to_name or 'Not visible'}"
        )
    else:
        issuer_name = from_party.get("name") or extract.get("vendor_name")
        issuer_uen = from_party.get("uen") or extract.get("issuer_gst_regno")
        bill_to_name = to_party.get("name") or extract.get("customer_name")
        bill_to_uen = to_party.get("uen")
        doc_kind = extract.get("doc_kind") or "invoice"
        summary_table = extract.get("summary_table") or []
        summary_str = "\n".join(
            f"- {s.get('category')}: {s.get('details')}"
            for s in summary_table
            if isinstance(s, dict)
        )

    # Two prompt variants: the standard one (name-match against the document) and
    # a playground variant (no real client to match against — pick the most
    # likely direction from document signals alone).
    if client_is_placeholder:
        instruction = (
            "You are an expert SG/MY accountant. The user is running in a "
            "DEV / playground session and has not loaded a real client profile. "
            "Pick the single most likely direction from the perspective of a "
            "typical small-business bookkeeper who just dropped this document "
            "into their inbox.\n\n"
            f"Document Details:\n"
            f"- Issuer/From: {issuer_name or 'Not visible'} (UEN: {issuer_uen or 'Not visible'})\n"
            f"- Billed To: {bill_to_name or 'Not visible'} (UEN: {bill_to_uen or 'Not visible'})\n"
            f"- Type of Document: {doc_kind}\n"
            "Visible Text Summary:\n"
            f"{summary_str}\n\n"
            "Heuristics (in priority order):\n"
            "1. If the document is a third-party issued bill, tax invoice, "
            "purchase order, statement of account, or anything that names the "
            "bookkeeper's company in the 'Billed To' or 'Bill To' field, it's "
            "a PURCHASE.\n"
            "2. If the document names the bookkeeper's company in the 'From' / "
            "'Issuer' / 'Sold To' field and bills an external party, it's a SALE.\n"
            "3. If the document is a receipt of payment, expense reimbursement, "
            "or petty cash slip, it's a PURCHASE.\n"
            "4. If the document is a quote or proforma without a clear direction, "
            "default to PURCHASE (a small-business bookkeeper uploads their own "
            "bills 10x more often than their own sales docs).\n\n"
            "Respond with a JSON object containing keys: 'direction' (must be "
            "one of: purchase, sales) and 'reason' (short explanation of the "
            "chosen direction)."
        )
    else:
        instruction = (
            "You are an expert SG/MY accountant. We are trying to determine if a document is a "
            "purchase or a sale from the perspective of our client.\n\n"
            f"Client Name: {client_name}\n"
            f"Client UEN: {client_uen or 'Not visible'}\n\n"
            "Document Details:\n"
            f"- Issuer/From: {issuer_name or 'Not visible'} (UEN: {issuer_uen or 'Not visible'})\n"
            f"- Billed To: {bill_to_name or 'Not visible'} (UEN: {bill_to_uen or 'Not visible'})\n"
            f"- Type of Document: {doc_kind}\n"
            "Visible Text Summary:\n"
            f"{summary_str}\n\n"
            "Rules:\n"
            "1. purchase: Client is the one paying. Either they are the 'Billed To' party, the recipient, "
            "or it is an expense claim / reimbursement for an employee.\n"
            "2. sales: Client is the one collecting. They are the 'Issuer/From' party selling goods/services.\n"
            "3. self_referential: The client is both the issuer and the recipient (e.g., dividend voucher, "
            "internal cash transfer, own billing).\n"
            "4. unknown: If it is completely ambiguous or the client name/UEN does not appear at all on the document.\n\n"
            "Analyze carefully. If the client name has a slight typo or abbreviation but matches either issuer or billed to, "
            "classify accordingly.\n"
            "Respond with a JSON object containing keys: 'direction' (must be one of: purchase, sales, self_referential, unknown) "
            "and 'reason' (short explanation)."
        )

    class _DirectionDecision(BaseModel):
        direction: str = Field(description="purchase | sales | self_referential | unknown")
        reason: str

    try:
        from invoice_processing.shared_libraries.gemini_call_config import default_llm_config
        from invoice_processing.shared_libraries.genai_client import make_client
        client = make_client()
        resp = client.models.generate_content(
            model=MODEL_LITE,
            contents=[instruction],
            config=default_llm_config(
                response_mime_type="application/json",
                response_schema=_DirectionDecision,
                temperature=0,
            ),
        )
        parsed = getattr(resp, "parsed", None)
        if parsed is None:
            text = (getattr(resp, "text", None) or "").strip()
            parsed = _DirectionDecision(**json.loads(text))

        direction = (parsed.direction or "unknown").strip().lower()
        if direction in ("purchase", "sales", "self_referential", "unknown"):
            logger.info("direction retry resolved to: %s (reason: %s)", direction, parsed.reason)
            return direction
    except Exception as exc:  # noqa: BLE001
        logger.warning("direction retry classification failed: %s", exc)

    return "unknown"


def _normalize_bundle(ctx, bundle: ExtractedInvoiceBundle) -> list[NormalizedInvoice]:
    """Reconcile + normalize every invoice in ``bundle`` into NormalizedInvoices.

    Shared by ``extract_invoice_document_node`` (first pass) and the extract reviewer's
    re-extract path (``_run_reviewer_loop``) so the normalization logic — totals
    reconcile, FX flag preservation, and the self-referential / unknown-direction
    review guards — lives in exactly one place. Reads direction / GST / base
    currency from ``ctx.state``; does NOT write state (the caller does).
    """
    direction = ctx.state.get(DIRECTION_KEY)
    # Structural direction for to_normalized must be "purchase" or "sales".
    # "self_referential" and "unknown" both default to "purchase" for row
    # structure, but are immediately flagged for review so the client is
    # never silently booked as its own vendor (self-referential case) or
    # routed without a confirmed side (unknown case).
    effective_direction = direction if direction in ("purchase", "sales") else "purchase"
    our_gst = _our_gst_registered_from_state(ctx.state)
    base_currency = _base_currency_from_state(ctx.state)

    normalized: list[NormalizedInvoice] = []
    for ex in bundle.invoices:
        # Hard-gate: drop phantom SOA-summary invoices hallucinated from the
        # SOA cover table (same predicate as to_normalized_bundle).
        if _is_soa_summary_invoice(ex):
            logger.warning(
                "hard-gate: dropping SOA-summary phantom invoice",
                extra={
                    "invoice_number": ex.invoice_number,
                    "line_count": len(ex.lines),
                    "reason": "all_lines_summary_shaped",
                },
            )
            continue
        ok, _detail = reconcile(ex)
        inv = to_normalized(
            ex,
            direction=effective_direction,
            our_gst_registered=our_gst,
            base_currency=base_currency,
            fx_rate=ex.fx_rate,
        )
        # Carry the classify document kind (e.g. "credit_note", "invoice", "receipt")
        # from state so exporters can apply the credit-note sign-flip at row-build time.
        # This is distinct from inv.doc_type which is the DIRECTION ("purchase"/"sales").
        inv.document_kind = ctx.state.get(DOC_TYPE_KEY)
        # to_normalized already sets reconciled=False when needs_fx_review is True;
        # only overwrite with the totals-reconcile result when FX has not already
        # forced reconciled=False, so we don't accidentally clear the FX flag.
        if not inv.needs_fx_review:
            inv.reconciled = ok
        # Self-referential / ambiguous direction guard (mirrors pipeline.py).
        if direction_needs_review(direction):
            append_direction_review_note(inv, direction)
        normalized.append(inv)
    return normalized


@node
async def categorize_node(ctx) -> Event:
    """Fill COA account codes per normalized invoice (COA from client profile)."""
    invoices = _normalized_from_state(ctx.state)
    coa = coa_from_state(ctx.state)
    cat_map = category_mapping_from_state(ctx.state)
    ent_mem = entity_memory_from_state(ctx.state)
    # tax_registered seeds the LLM prompt with GST context so the model knows
    # not to fight the deterministic tax master gate (§0.5-C).
    # None default is deliberate: signals "unknown" to the LLM prompt branch so
    # the model hedges rather than assuming registered.  Distinct from tax_node's
    # `True` default, which drives the classifier's safe fallback for registered clients.
    tax_registered: bool | None = ctx.state.get("tax_registered")
    # Region context for the LLM — Phase 8 / multi-country. Surfaces in the
    # LLM prompt as {client_region?} (auto-injected by ADK from session state).
    client_region: str = ctx.state.get("region") or ctx.state.get("client_region") or ""
    client_currency: str = (
        ctx.state.get("base_currency") or ctx.state.get("client_currency") or ""
    ).upper()

    # ADR-0004: LLM guesses are PROVISIONAL — they flow into the invoice and
    # then through the HITL approval gate.  Auto-persisting them into
    # entity_memory / category_mapping / Corrections here would bypass the
    # human-approve-with-edit path that is the ONLY sanctioned learning path
    # (slack_runner._persist_corrections).  Do NOT add persistence here.
    for inv in invoices:
        CATEGORIZE_FN(
            inv,
            coa=coa,
            category_mapping=cat_map,
            entity_memory=ent_mem,
            model=MODEL_LITE,
            use_llm=True,
            tax_registered=tax_registered,
            client_region=client_region,
            client_currency=client_currency,
        )

    ctx.state[NORMALIZED_KEY] = _guard_state_payload(
        NORMALIZED_KEY, [_inv_to_dict(i) for i in invoices]
    )
    emit_account_flagged_from_state(ctx.state)
    return Event(output={"count": len(invoices)})


@node
async def resolve_jurisdiction_node(ctx) -> Event:
    """Resolve tax jurisdiction from session state — single authority for WS2a.

    Multi-country support: reads ``state["region"]`` + ``state["base_currency"]``
    + ``state["supplier_country"]`` / ``state["customer_country"]`` and writes
    ``state["tax_jurisdiction"]`` + ``state["tax_system_hint"]`` +
    ``state["jurisdiction_rates"]``. ADK web's State tab surfaces these keys so
    the operator can see exactly which rule set was applied.

    Per ADK best practice (Sessions/State docs): region lives in user-level
    state (``state["region"]``) seeded by the profile callback. The router
    is a thin pure function — no LLM, no I/O. Pure data in / pure data out.

    WS2a: party countries (supplier_country / customer_country) are seeded HERE
    from the normalized invoices so that cross-border detection is complete
    before the single ``_resolve_jurisdiction_fn`` call.  ``tax_node`` reads
    the five jurisdiction keys from state via ``_resolution_from_state`` and
    must NOT re-resolve.
    """
    # Seed supplier_country / customer_country from normalized invoices so that
    # cross-border determination has complete inputs on this single resolve call.
    _populate_party_countries_from_invoices(ctx.state)
    resolution = _resolve_jurisdiction_fn(ctx.state)
    _write_jurisdiction_to_state(ctx.state, resolution)
    return Event(
        output={
            "tax_jurisdiction": resolution.jurisdiction.code,
            "tax_system": resolution.jurisdiction.tax_system,
            "flag_for_human": resolution.jurisdiction.flag_for_human,
            "client_region": resolution.client_region,
        }
    )


def _populate_party_countries_from_invoices(state: dict) -> None:
    """Copy supplier.country / customer.country from normalized invoices to state.

    The extract node writes country into ``NormalizedInvoice.supplier.country``
    but does NOT push it into session state. The jurisdiction router reads
    from state, so we bridge here. Called from the document_workflow_driver
    between extract and tax.
    """
    invoices = _normalized_from_state(state)
    if not invoices:
        return
    inv = invoices[0]  # one-invoice-at-a-time is the spine's invariant
    if inv.supplier and inv.supplier.country:
        state.setdefault(SUPPLIER_COUNTRY_KEY, _norm_country(inv.supplier.country))
    if inv.customer and inv.customer.country:
        state.setdefault(CUSTOMER_COUNTRY_KEY, _norm_country(inv.customer.country))


def _norm_country(value: object) -> Optional[str]:
    """Coerce a free-form country string to a 2-letter code (SG/MY/...).

    Tiny local copy of ``jurisdiction._norm_country`` so this module has no
    circular import on the public helper. Behaviour matches upstream.
    """
    if not value:
        return None
    s = str(value).strip().upper()
    if not s:
        return None
    aliases = {"SG": "SG", "SGP": "SG", "SINGAPORE": "SG",
               "MY": "MY", "MYS": "MY", "MALAYSIA": "MY", "MSIA": "MY"}
    return aliases.get(s, s[:2] if len(s) >= 2 else s)


@node
async def tax_node(ctx) -> Event:
    """Region-aware per-line tax classification (LLM-first, Python-guarded).

    Replaces the previous SG-only ``TaxClassifier()`` call. Flow:

    1. Read the already-resolved jurisdiction from state (written by
       ``resolve_jurisdiction_node``, the single authority per WS2a).
    2. For each invoice, call ``tax_reasoning.reason_one_invoice`` which:
       * Builds an LLM prompt with jurisdiction context (region, tax system,
         standard rate, supplier country) via state templating.
       * Asks the LLM for a structured per-line decision
         (tax_treatment / tax_confidence / tax_reason).
       * Validates the LLM's math with a Python rate guard
         (no per-line signal spaghetti — just arithmetic + tolerance).
       * Falls back to the deterministic ``TaxClassifier`` for SG invoices
         when the LLM is unreachable (preserves C6–C8 golden behaviour).
    3. Emits the jurisdiction keys from state so ADK web's State tab and
       trace events show the rule set used.

    WS2a invariant: this node MUST NOT call ``_resolve_jurisdiction_fn`` or
    ``_populate_party_countries_from_invoices``.  If ``tax_jurisdiction`` is
    absent, ``_resolution_from_state`` raises ``RuntimeError`` so the missing
    ``resolve_jurisdiction_node`` is loud, not silent.
    """
    # Read the jurisdiction that resolve_jurisdiction_node already wrote.
    # Raises RuntimeError if tax_node ran before resolve_jurisdiction_node.
    resolution = _resolution_from_state(ctx.state)

    invoices = _normalized_from_state(ctx.state)
    flagged_total = 0
    for inv in invoices:
        outcome = _reason_one_invoice(
            inv,
            state=ctx.state,
            jurisdiction_resolution=resolution,
        )
        flagged_total += outcome.flagged_count

    ctx.state[NORMALIZED_KEY] = _guard_state_payload(
        NORMALIZED_KEY, [_inv_to_dict(i) for i in invoices]
    )
    return Event(
        output={
            "count": len(invoices),
            "tax_jurisdiction": resolution.jurisdiction.code,
            "tax_system": resolution.jurisdiction.tax_system,
            "flagged_lines": flagged_total,
            "cross_border": resolution.jurisdiction.cross_border,
        }
    )


# --------------------------------------------------------------------------- #
# Extract reviewer — the "smart inspector" between extraction and categorization
#
# Design: the happy path spends ZERO extra LLM. ``review_extraction_node`` runs
# the cheap deterministic ``detect_struggle``; if nothing tripped it waves the
# document straight through to categorize (``REVIEWER_FN`` is NOT called). Only
# when a signal fires does it spend a bounded number of critic calls (§9.3) to
# either re-extract with a hint or — on circuit-break (§9.5) — ask the human
# mid-flow via a SECOND ``RequestInput`` (distinct ``:review`` interrupt id).
# --------------------------------------------------------------------------- #


def _is_soft_only(reasons: list[str]) -> bool:
    """Return True iff every reason in ``reasons`` is a known SOFT signal.

    Empty → False (no trip → no critic call; the zero-signal happy path must
    never be routed to the critic).

    FAIL-SAFE: any reason string that does NOT start with a prefix in
    ``SOFT_SIGNAL_PREFIXES`` is classified as HARD and causes this function to
    return False, regardless of the other reasons present.  Unknown / future
    signals escalate conservatively — the safe direction.
    """
    if not reasons:
        return False
    return all(
        any(r.startswith(prefix) for prefix in SOFT_SIGNAL_PREFIXES)
        for r in reasons
    )


def _is_unreconciled_only_detect_reasons(reasons: list[str]) -> bool:
    """True when every ``detect_struggle`` reason is unreconciled (WS4)."""
    return bool(reasons) and all(r.startswith("unreconciled:") for r in reasons)


def _is_reconcile_only_needs_review_reasons(reasons: list[str]) -> bool:
    """True when ``_needs_review`` reasons are only not-reconciled (WS4).

    A reason that mentions ``direction`` (e.g. "direction unknown") is NOT
    a reconcile-totals problem — it is a structural ambiguity that a totals-
    focused re-extract cannot fix.  Exclude it so direction-uncertain docs
    escalate to the reviewer rather than triggering a wasted re-read.
    """
    return bool(reasons) and all(
        ": not reconciled (" in r and "direction" not in r.lower()
        for r in reasons
    )


def detect_struggle(state: dict) -> tuple[bool, list[str]]:
    """Pure, deterministic struggle detector — NO LLM, NO network.

    Returns ``(tripped, reasons)`` where ``reasons`` is a list of STABLE machine
    reason strings (used to steer the critic and for audit). A clean extraction
    returns ``(False, [])`` so the happy path never invokes ``REVIEWER_FN``.

    §0.5-C GUARD: when an invoice's ``our_gst_registered`` is False, ALL
    tax/GST-shaped signals are skipped for that invoice (a None/0 ``gst_amount``
    is NORMAL for a non-registered client, not a struggle) — so a non-registered
    client's "missing tax" never trips the reviewer.
    """

    invoices = _normalized_from_state(state)

    reasons: list[str] = []

    # NOTE: jurisdiction + COA + currency signals are NOT computed here.
    # detect_struggle runs at lane position 2 (review_extraction), BEFORE
    # categorize (writes account_code) and resolve_jurisdiction (writes
    # tax_jurisdiction). Reading those state keys here always saw empty
    # values and over-flagged every commercial doc. The genuine conditions
    # (blank/not-in-COA account codes, unresolved jurisdiction, currency
    # mismatch) are caught post-resolution at the terminal gate in
    # _needs_review instead.

    # Signal #1: bundle_empty — normalization produced zero invoices.
    if not invoices:
        reasons.append("bundle_empty")

    # Read doc_type now; Signal #4 is evaluated AFTER the per-invoice loop so
    # weak_extract can be computed from signals gathered in that loop.
    doc_type = (state.get(DOC_TYPE_KEY) or "").strip().lower()

    if "software" in state:
        sw = resolve_software(state.get("software"))
        if sw.flagged:
            reasons.append(f"software_unresolved: {sw.reason}")

    # Signal #5: low_classify_confidence — classifier hedged.
    conf = state.get(CLASSIFY_CONFIDENCE_KEY)
    if conf is not None and conf < CLASSIFY_CONFIDENCE_FLOOR:
        reasons.append("low_classify_confidence")

    for idx, inv in enumerate(invoices):
        label = inv.invoice_number or f"invoice #{idx + 1}"

        # Signal #2: lines_empty — an invoice with no ledger lines.
        if not inv.lines:
            reasons.append(f"lines_empty: {label}")

        # Signal #3: unreconciled — totals/FX/direction reconcile failed
        # (already computed upstream; carry the note for the critic).
        if not inv.reconciled:
            note = inv.reconcile_note or "totals do not reconcile"
            totals_ok = (
                note.strip().lower().startswith("reconciled")
                or "· ok" in note.lower()
            )
            if not totals_ok:
                reasons.append(f"unreconciled: {label} ({note})")
            elif (
                "direction unknown" in note
                or "self-referential" in note
                or "direction not confirmed" in note
            ):
                reasons.append(f"direction_uncertain: {label}")

        # Signal #6: missing_required — a core identifier/total is absent.
        # §0.5-C: doc_total is a tax/GST-shaped total only insofar as a
        # non-registered client's bill may legitimately omit a GST-inclusive
        # grand total; we still require invoice_number + invoice_date for ALL
        # clients (identity), but skip the doc_total requirement for a
        # non-registered client so a "missing tax total" never trips it.
        missing: list[str] = []
        if not inv.invoice_number:
            missing.append("invoice_number")
        if not inv.invoice_date:
            missing.append("invoice_date")
        if inv.our_gst_registered and inv.doc_total is None:
            missing.append("doc_total")
        if missing:
            reasons.append(f"missing_required: {label} ({', '.join(missing)})")

    # Signal #4: doc_type_other — quality-gated (ADR-0017 Lever 1).
    # For 'other' and 'expense_claim' doc types, only escalate when the
    # extraction is also weak (bundle_empty, lines_empty, unreconciled, or
    # missing_required).  A clean, reconciled 'other'/'expense_claim' posts
    # without a pause — the label alone is not sufficient reason to escalate.
    if doc_type in ("other", "expense_claim"):
        weak_extract = (
            "bundle_empty" in reasons
            or any(r.startswith("lines_empty") for r in reasons)
            or any(r.startswith("unreconciled") for r in reasons)
            or any(r.startswith("missing_required") for r in reasons)
        )
        if weak_extract:
            reasons.append("doc_type_other")

    # Lever 3 (ADR-0017 §2) — processable_false HARD signal.
    # When the classifier determined the document has no bookable financial
    # content at all, append this hard signal.  It is deliberately NOT in
    # SOFT_SIGNAL_PREFIXES so _is_soft_only() returns False, which means:
    #   • The Lever 2 critic (LLM-as-judge) cannot clear it.
    #   • The Lever 4 familiarity gate (below) is bypassed entirely.
    # Absence of the key is treated as processable=True (backwards-compatible
    # with state dicts written before Lever 3 was deployed).
    if state.get(CLASSIFY_PROCESSABLE_KEY) is False:
        reasons.append("processable_false")

    # WS-6.4 — trend observability before familiarity may suppress return value.
    if reasons or any(not inv.reconciled for inv in invoices):
        emit_from_struggle_state(state, reasons)

    # Lever 4 (ADR-0017 §6) — familiarity gate: if ALL remaining signals are
    # SOFT and the client has seen this doc shape >= FAMILIARITY_THRESHOLD
    # times without correction, suppress the soft reasons so the run proceeds
    # on the confident path with NO critic call and NO human pause.
    #
    # Most-specific-key gating (closes 4c reset defeat):
    #   • Vendor identifiable → consult ONLY the compound ``doc_type:vendor``
    #     key.  Suppressing on the bare key too would let the surviving bare
    #     count mask a vendor-level correction (which only resets the compound).
    #   • No vendor identifiable → fall back to the bare ``doc_type`` key.
    # Hard signal present → _is_soft_only is False → bypassed entirely.
    if reasons and _is_soft_only(reasons):
        fam_map: dict = state.get(FAMILIARITY_KEY) or {}
        if fam_map:
            # Derive dominant vendor from first normalized invoice.
            dominant_vendor: Optional[str] = None
            if invoices:
                first_inv = invoices[0]
                doc_dir = (state.get(DIRECTION_KEY) or "purchase").strip().lower()
                if doc_dir == "purchase":
                    party = getattr(first_inv, "supplier", None)
                else:
                    party = getattr(first_inv, "customer", None)
                if party is not None:
                    dominant_vendor = getattr(party, "name", None)

            if dominant_vendor:
                # Vendor known: use compound key only — most specific.
                dtv_key = f"{doc_type}:{dominant_vendor}"
                dtv_count = (fam_map.get(dtv_key) or {}).get("seen_count", 0)
                if dtv_count >= FAMILIARITY_THRESHOLD:
                    return (False, [])
            else:
                # No vendor: fall back to bare doc_type key.
                dt_count = (fam_map.get(doc_type) or {}).get("seen_count", 0)
                if dt_count >= FAMILIARITY_THRESHOLD:
                    return (False, [])

    return (bool(reasons), reasons)


def _reviewer_llm(state: dict, reasons: list[str], *, model: str) -> dict:
    """Default extract critic — a small Gemini reader on ``model`` (MODEL_LITE).

    Re-reads the same PDF + the current extraction and returns a STRUCTURED
    verdict dict. §0.5-B: the instruction REQUIRES an explicit verdict ("never
    end with only a tool call / always return the verdict"); an empty or
    unparseable response degrades to ``user_clarify`` (safe human escalation),
    never a crash.

    Returns a dict with keys: ``verdict`` (one of REVIEW_VERDICT_OK / _HINTS /
    _CLARIFY), ``hint`` (re-extract steering when verdict==hints_needed),
    ``question`` (human-facing prompt when verdict==user_clarify).

    NOTE: this default talks to Gemini; unit tests swap ``REVIEWER_FN`` for a
    fake so no network is touched. Kept intentionally small — the bounded loop
    in ``_run_reviewer_loop`` owns retry/ceiling policy, not this callable.
    """
    from google.genai import types as _genai_types

    from invoice_processing.shared_libraries.genai_client import make_client

    record_summary: list[str] = []
    for raw in state.get(DOCUMENT_RECORDS_KEY) or []:
        if isinstance(raw, dict):
            record_summary.append(
                f"labels={len(raw.get('labeled_fields') or [])} "
                f"lines={len(raw.get('line_items') or [])} "
                f"tables={len(raw.get('tables') or [])} "
                f"parties={len(raw.get('parties') or [])}"
            )
        else:
            record_summary.append(
                f"labels={len(raw.labeled_fields)} lines={len(raw.line_items)} "
                f"tables={len(raw.tables)} parties={len(raw.parties)}"
            )

    capture_json = json.dumps(state.get(DOCUMENT_RECORDS_KEY) or [], default=str)
    booking_json = json.dumps(state.get(BOOKING_PROPOSALS_KEY) or [], default=str)

    instruction = (
        "You are a meticulous bookkeeping QA reviewer. An automated reader "
        "extracted an invoice/receipt and a deterministic checker flagged these "
        "concerns:\n"
        + "\n".join(f"- {r}" for r in reasons)
        + "\n\nPhase 1 capture JSON (ground truth for what is visible on the document):\n"
        + capture_json
        + "\n\nBooking proposal JSON (posting granularity — must reconcile to capture footer):\n"
        + booking_json
        + "\n\nPhase 1 capture summary:\n"
        + "\n".join(f"- {s}" for s in record_summary)
        + "\n\nNormalized extraction JSON:\n"
        + json.dumps(state.get(NORMALIZED_KEY) or [], default=str)
        + "\n\nDecide ONE verdict and ALWAYS return it explicitly — never end "
        "with only a tool call and never reply with empty text:\n"
        f"- '{REVIEW_VERDICT_OK}': the extraction is acceptable as-is. "
        "Use this when the flagged concerns are soft signals only (e.g. uncertain "
        "doc-type label, low classifier confidence, ambiguous direction) AND the "
        "extraction reconciles, required fields (invoice_number, invoice_date, "
        "doc_total where applicable) are present, and the booking lines are "
        "coherent. A novel or unfamiliar doc type alone is NOT a reason to "
        "escalate — post from first principles if the numbers add up.\n"
        f"- '{REVIEW_VERDICT_HINTS}': a re-extraction with a specific hint would "
        "likely fix it; provide a short 'hint'.\n"
        f"- '{REVIEW_VERDICT_CLARIFY}': only a human can resolve it; provide a "
        "short 'question' for the accountant. Reserve this for genuine problems: "
        "amounts that do not reconcile, illegible key fields, or substantive "
        "ambiguity that cannot be resolved from the document alone.\n"
        "Respond as JSON with keys: verdict, hint, question."
    )

    class _ReviewVerdict(BaseModel):
        verdict: str = Field(description="ok | hints_needed | user_clarify")
        hint: Optional[str] = None
        question: Optional[str] = None

    try:
        client = make_client()
        resp = client.models.generate_content(
            model=model,
            contents=[instruction],
            config=_genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_ReviewVerdict,
            ),
        )
        parsed = getattr(resp, "parsed", None)
        if parsed is None:
            text = (getattr(resp, "text", None) or "").strip()
            if not text:
                raise ValueError("empty reviewer response")
            parsed = _ReviewVerdict(**json.loads(text))
        verdict = (parsed.verdict or "").strip()
        if verdict not in (REVIEW_VERDICT_OK, REVIEW_VERDICT_HINTS, REVIEW_VERDICT_CLARIFY):
            raise ValueError(f"unrecognized reviewer verdict {verdict!r}")
        return {"verdict": verdict, "hint": parsed.hint, "question": parsed.question}
    except Exception as exc:  # noqa: BLE001 — degrade to safe human escalation (§0.5-B)
        logger.warning("extract reviewer degraded to user_clarify: %s", exc)
        return {"verdict": REVIEW_VERDICT_CLARIFY, "hint": None, "question": None}


REVIEWER_FN = _reviewer_llm


def _run_reviewer_loop(ctx, reasons: list[str], pdf_bytes: bytes, mime_type: str = "application/pdf") -> str:
    """Bounded IN-NODE reviewer loop (NOT a graph cycle). Returns a verdict.

    §9.3 ceiling: at most ``REVIEW_MAX_REVIEWS`` critic calls +
    ``REVIEW_MAX_REEXTRACTS`` re-extract. ``hints_needed`` (within the re-extract
    cap) re-runs Phase 1+2 with the hint appended,
    re-detects, and continues. Ceiling hit / hints exhausted / two reviews still
    bad → ``user_clarify`` (circuit-break to a human, §9.5). On ``ok`` the loop
    returns immediately. Tracks ``review_attempts`` (≤2) and
    ``review_reextract_count`` (≤1) in state for audit.
    """
    attempts = 0
    reextracts = 0
    question: Optional[str] = None

    while attempts < REVIEW_MAX_REVIEWS:
        attempts += 1
        ctx.state["review_attempts"] = attempts
        result = REVIEWER_FN(ctx.state, reasons, model=MODEL_LITE) or {}
        verdict = result.get("verdict") or REVIEW_VERDICT_CLARIFY

        if verdict == REVIEW_VERDICT_OK:
            return REVIEW_VERDICT_OK

        if verdict == REVIEW_VERDICT_HINTS and reextracts < REVIEW_MAX_REEXTRACTS:
            hint = result.get("hint") or ""
            _reextract_with_hint(ctx, hint, pdf_bytes, mime_type)
            reextracts += 1
            ctx.state["review_reextract_count"] = reextracts
            # Re-detect on the fresh extraction; if it's now clean we still loop
            # once more so the critic can confirm (bounded by REVIEW_MAX_REVIEWS).
            tripped, reasons = detect_struggle(ctx.state)
            if not tripped:
                return REVIEW_VERDICT_OK
            continue

        # verdict == user_clarify, OR hints_needed with the re-extract cap hit:
        # circuit-break to a human (§9.5).
        question = result.get("question")
        break

    # Ceiling / hints exhausted / two reviews still bad → human.
    ctx.state["review_question"] = question or _review_clarify_question(reasons)
    return REVIEW_VERDICT_CLARIFY


def _reextract_with_hint(ctx, hint: str, pdf_bytes: bytes, mime_type: str = "application/pdf") -> None:
    """Re-run extraction with ``hint`` appended, rewrite state."""
    ctx.state["review_hint"] = hint
    result = EXTRACT_INVOICE_DOCUMENT_FN(
        pdf_bytes,
        mime_type,
        doc_type=ctx.state.get(DOC_TYPE_KEY) or "invoice",
        direction=ctx.state.get(DIRECTION_KEY) or "auto",
        our_gst_registered=_our_gst_registered_from_state(ctx.state),
        base_currency=_base_currency_from_state(ctx.state),
        client_name=ctx.state.get("client_name"),
        client_uen=ctx.state.get("client_uen"),
        hint=hint,
        model=MODEL_READ,
    )
    _apply_invoice_process_result(ctx, result)


def _review_clarify_question(reasons: list[str]) -> str:
    """Human-facing prompt summarizing why the extraction needs clarification."""
    header = (
        "I had trouble reading this document confidently. Could you confirm or "
        "re-extract it? The reader flagged:"
    )
    bullets = "\n".join(f"  • {r}" for r in reasons)
    return f"{header}\n{bullets}"


@node(rerun_on_resume=True)
async def review_extraction_node(ctx):
    """Mid-flow extract reviewer (async generator — mirrors ``approval_gate``).

    ``rerun_on_resume=True`` (unlike the auto-wrapped default of False) so that,
    on the human's resume, ADK RE-RUNS this node with the response in
    ``ctx.resume_inputs`` (keyed by interrupt id) — letting the SAME node apply
    the ``ReviewClarifyDecision`` and then fall through to categorize. (The
    terminal ``approval_gate`` keeps the default fast-forward behavior, handing
    its decision to the separate ``apply_decision_node``.)

    Runs the deterministic ``detect_struggle`` and ALWAYS records its reasons in
    ``state[REVIEW_REASON_KEY]``. If nothing tripped → writes
    ``state[REVIEW_VERDICT_KEY]=REVIEW_VERDICT_OK`` and returns (happy path: ZERO
    LLM, ``REVIEWER_FN`` NOT called), falling through to ``categorize_node``.

    If tripped → runs the bounded ``_run_reviewer_loop``. On ``user_clarify`` it
    ``yield``s a SECOND ``RequestInput`` whose interrupt id is the terminal gate's
    id suffixed ``:review`` (so the two pauses are distinct and ``hitl.py`` resume
    works unchanged); on resume it applies the human's :class:`ReviewClarifyDecision`
    (``reextract_as`` re-extracts with the hint; ``reject`` empties
    ``NORMALIZED_KEY``; ``confirm_as_is`` waves through). Either way the document
    then falls through to ``categorize_node``.
    """
    review_interrupt_id = f"{_approval_interrupt_id(ctx.state)}:review"

    # RESUME PATH: ADK re-runs this WAITING node with the human's response in
    # ``ctx.resume_inputs`` (keyed by interrupt id — the same mechanism the auth
    # gate uses). Apply the ReviewClarifyDecision and fall through to categorize
    # WITHOUT re-running the (LLM) reviewer loop or re-yielding the interrupt.
    resume = getattr(ctx, "resume_inputs", None) or {}
    if review_interrupt_id in resume:
        # Only re-read the source PDF when the human actually asked for a
        # re-extract — the happy/confirm/reject paths never touch the artifact.
        pdf_bytes = await _maybe_load_pdf(ctx, resume[review_interrupt_id])
        _apply_review_clarify(ctx, resume[review_interrupt_id], pdf_bytes)
        ctx.state[REVIEW_VERDICT_KEY] = REVIEW_VERDICT_CLARIFY
        return

    tripped, reasons = detect_struggle(ctx.state)
    ctx.state[REVIEW_REASON_KEY] = reasons

    if not tripped:
        # Happy path: ZERO extra LLM, and we never even touch the artifact.
        ctx.state[REVIEW_VERDICT_KEY] = REVIEW_VERDICT_OK
        return

    # WS4: unreconciled-only → one totals-focused re-extract before critic/HITL.
    if (
        _is_unreconciled_only_detect_reasons(reasons)
        and not ctx.state.get(RECONCILE_REEXTRACT_ATTEMPTED_KEY)
    ):
        pdf_bytes, mime_type = await _load_pdf_bytes(ctx)
        _reextract_with_hint(ctx, RECONCILE_REREAD_HINT, pdf_bytes, mime_type)
        ctx.state[RECONCILE_REEXTRACT_ATTEMPTED_KEY] = True
        tripped, reasons = detect_struggle(ctx.state)
        ctx.state[REVIEW_REASON_KEY] = reasons
        if not tripped:
            ctx.state[REVIEW_VERDICT_KEY] = REVIEW_VERDICT_OK
            return

    # Lever 2 (ADR-0017 §3): when EVERY tripped reason is soft, run the critic.
    # On REVIEW_VERDICT_OK the doc falls through with no human pause.
    # Any hard signal bypasses this branch entirely — the critic cannot clear
    # hard signals regardless of what it would return.
    if _is_soft_only(reasons):
        pdf_bytes, mime_type = await _load_pdf_bytes(ctx)
        verdict = _run_reviewer_loop(ctx, reasons, pdf_bytes, mime_type)
        ctx.state[REVIEW_VERDICT_KEY] = verdict
        if verdict == REVIEW_VERDICT_OK:
            # Critic cleared the soft signals — fall through, no human pause.
            return
        # Critic returned CLARIFY or HINTS exhausted → escalate to human.
        review_q = ctx.state.get("review_question") or _review_clarify_question(reasons)
        ctx.state["review_question"] = review_q
        yield RequestInput(
            interrupt_id=review_interrupt_id,
            message=review_q,
            response_schema=ReviewClarifyDecision,
        )
        return

    # Hard signal(s) present — run the bounded reviewer loop solely to attempt
    # a deterministic auto-fix via the HINTS re-extraction path.  After the loop
    # returns, re-run detect_struggle on the (possibly updated) state.
    #
    # CRITICAL: the critic's OK verdict alone CANNOT clear a hard signal.
    # We re-check detect_struggle deterministically:
    #   • If hard signals still present  → escalate to human regardless of verdict.
    #   • If state is now clean or soft-only AND verdict is OK → proceed (auto-fix
    #     via HINTS re-extraction actually fixed the doc).
    #   • If verdict is CLARIFY regardless of detect_struggle → escalate.
    pdf_bytes, mime_type = await _load_pdf_bytes(ctx)
    verdict = _run_reviewer_loop(ctx, reasons, pdf_bytes, mime_type)

    # Re-check the extraction state deterministically after the loop.
    recheck_tripped, recheck_reasons = detect_struggle(ctx.state)
    hard_still_present = recheck_tripped and not _is_soft_only(recheck_reasons)

    if hard_still_present or verdict == REVIEW_VERDICT_CLARIFY:
        # Either a hard signal survives (LLM OK cannot override this) or the
        # critic explicitly asked for human clarification.
        ctx.state[REVIEW_VERDICT_KEY] = REVIEW_VERDICT_CLARIFY
        ctx.state[REVIEW_REASON_KEY] = recheck_reasons  # update to post-fix state
        review_q = ctx.state.get("review_question") or _review_clarify_question(recheck_reasons)
        ctx.state["review_question"] = review_q
        yield RequestInput(
            interrupt_id=review_interrupt_id,
            message=review_q,
            response_schema=ReviewClarifyDecision,
        )
        return

    # State is now clean (or soft-only) and critic returned OK — auto-fix succeeded.
    ctx.state[REVIEW_VERDICT_KEY] = verdict
    ctx.state[REVIEW_REASON_KEY] = recheck_reasons


async def _maybe_load_pdf(ctx, decision) -> tuple[bytes, str]:
    """Load source bytes + mime when resume action is re-extract."""
    data = decision if isinstance(decision, dict) else {}
    if data.get("action") == "reextract_as":
        return await _load_pdf_bytes(ctx)
    return b"", "application/pdf"


def _apply_review_clarify(ctx, decision, pdf_payload: bytes | tuple[bytes, str]) -> None:
    """Apply the human's ReviewClarifyDecision resume payload to the run state."""
    if isinstance(pdf_payload, tuple):
        pdf_bytes, mime_type = pdf_payload
    else:
        pdf_bytes, mime_type = pdf_payload, "application/pdf"
    data = decision if isinstance(decision, dict) else {}
    action = data.get("action")
    ctx.state["review_clarify_action"] = action

    if action == "reject":
        ctx.state[NORMALIZED_KEY] = []
        return
    if action == "reextract_as":
        _reextract_with_hint(ctx, data.get("hint") or "", pdf_bytes, mime_type)
        return
    # confirm_as_is (or missing/unknown action): wave the current extraction
    # through unchanged.


@node
async def extract_bank_node(ctx) -> Event:
    """Extract a bank statement into a list of BankStatements.

    Digital PDFs (pdfplumber text) use ``MODEL_LITE``; scanned/image paths use
    ``MODEL_STD`` for stronger multimodal OCR.
    """
    data, mime_type = await _load_pdf_bytes(ctx)
    result = EXTRACT_BANK_FN(
        data,
        mime_type,
        digital_model=MODEL_LITE,
        vision_model=MODEL_STD,
    )
    # extract_bank_statement returns (ExtractedBankStatement, mode_used); fakes
    # may return just the statement.
    if isinstance(result, tuple):
        ex_bank, mode_used = result
    else:
        ex_bank, mode_used = result, None

    statements = to_bank_statements(ex_bank, mode_used=mode_used)
    ctx.state[BANK_STATEMENTS_KEY] = _guard_state_payload(
        BANK_STATEMENTS_KEY, [_bank_to_dict(s) for s in statements]
    )
    return Event(output={"count": len(statements)})


@node
async def apply_decision_node(ctx, node_input=None) -> Event:
    """Apply the human's ApproveDecision (resume node_input) to the run state.

    The node has three observable branches, distinguished in
    ``state[APPROVAL_STATUS_KEY]`` for audit:

    * **Auto-approved** (no HITL): ``node_input`` is ``None`` or has no
      ``decision`` key. The node returns immediately and does NOT touch
      ``state`` — :data:`APPROVAL_STATUS_KEY` stays unset (the ``approval_gate``
      sets it to ``"auto_approved"`` itself in this case).
    * **Approve** (human hit Approve): records ``APPROVAL_STATUS_KEY='approve'``
      and leaves ``state[NORMALIZED_KEY]`` unchanged. The audit trail can
      therefore distinguish a human-click approval from an auto-approval even
      though both pass the invoices through to the consolidate/deliver spine.
    * **Edit** (human edited one or more lines): records
      ``APPROVAL_STATUS_KEY='edit'`` and mutates ``invoices[0]['lines']`` in
      place. **Single-invoice assumption:** the spine pauses once per session
      and a session holds at most one document, so ``len(invoices) == 1`` is
      the expected shape. Multi-invoice bundles per pause are OUT OF SCOPE for
      this node because the current HITL DTO (see :class:`ApproveDecision`)
      carries no ``invoice_index`` field. If extraction ever fans out into >1
      invoice during a HITL pause, this node logs a WARNING and still applies
      edits to ``invoices[0]`` only — the other invoice(s) are left unchanged.
      Adding an ``invoice_index`` to the edit DTO is a forward-compatible API
      change that should be coordinated with the Task 7 modal builder.
    * **Reject**: records ``APPROVAL_STATUS_KEY='reject'`` and clears
      ``state[NORMALIZED_KEY]`` so the consolidate/deliver spine produces
      nothing.

    Inserted between ``approval_gate`` and ``route_node`` so ADK delivers the
    resume ``ApproveDecision`` (the user's response to the ``RequestInput``) as
    this node's ``node_input`` (see tests/test_hitl_roundtrip.py — the gate's
    downstream node already receives the decision in its ``node_input``).
    """
    decision = node_input if isinstance(node_input, dict) else {}
    choice = decision.get("decision")
    if not choice:
        return Event(output={"decision": "auto_approved"})

    ctx.state[APPROVAL_STATUS_KEY] = choice

    if choice == "reject":
        ctx.state[NORMALIZED_KEY] = []
        return Event(output={"decision": "reject"})

    if choice == "edit":
        edits = (decision.get("edits") or {}).get("lines") or []
        invoices = ctx.state.get(NORMALIZED_KEY) or []
        if len(invoices) > 1:
            logger.warning(
                "apply_decision_node: edit DTO has no invoice_index; "
                "applying %d edits to invoices[0] only — %d other invoice(s) unchanged",
                len(edits), len(invoices) - 1,
            )
        if invoices:
            lines = invoices[0].get("lines") or []
            for e in edits:
                i = e.get("index")
                if isinstance(i, int) and 0 <= i < len(lines):
                    for field in EDITABLE_LINE_FIELDS:
                        if e.get(field) is not None:
                            lines[i][field] = e[field]
                    if e.get("account_code") is not None:
                        lines[i]["account_flagged"] = False
                        lines[i]["account_flag_reason"] = None
                        lines[i]["account_alternative_codes"] = []
            ctx.state[NORMALIZED_KEY] = _guard_state_payload(NORMALIZED_KEY, invoices)
    return Event(output={"decision": choice})


@node
async def route_node(ctx) -> Event:
    """Compute FY + sheet/direction routing metadata (NO GCS)."""
    fye_month, _defaulted = _effective_fye_month(ctx.state)
    client_id = ctx.state.get("client_id") or "unknown"
    doc_type = (ctx.state.get(DOC_TYPE_KEY) or "").strip().lower()
    direction = ctx.state.get(DIRECTION_KEY)

    routes: list[dict] = []

    if doc_type == "bank_statement":
        doc_date = _bank_run_representative_date(ctx.state)
        for _s in _bank_from_state(ctx.state):
            routes.append(
                _route_to_dict(
                    route_document(
                        doc_type="bank_statement",
                        direction=None,
                        doc_date=doc_date,
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


def _needs_review(state: dict) -> tuple[bool, list[str]]:
    """Return ``(needs_review, reasons)`` by inspecting normalized invoices.

    A document needs human approval when ANY invoice/line is flagged, not
    reconciled, or carries a tax confidence below
    :data:`APPROVAL_CONFIDENCE_THRESHOLD`. Reasons are human-readable strings
    used to build the approval prompt.
    """
    reasons: list[str] = []

    jurisdiction_review = state.get(JURISDICTION_REVIEW_REASON_KEY)
    if jurisdiction_review:
        reasons.append(str(jurisdiction_review))
    elif (
        state.get(TAX_JURISDICTION_KEY) == JURISDICTION_AMBIGUOUS
        and state.get(FLAG_FOR_HUMAN_KEY)
    ):
        reasons.append(
            "Client tax region not set — please confirm region in client settings"
        )

    jurisdiction_ambiguous = state.get(TAX_JURISDICTION_KEY) == JURISDICTION_AMBIGUOUS

    for idx, inv in enumerate(_normalized_from_state(state)):
        label = inv.invoice_number or f"invoice #{idx + 1}"
        if not inv.reconciled:
            note = inv.reconcile_note or "totals do not reconcile"
            reasons.append(f"{label}: not reconciled ({note})")
        # Currency vs jurisdiction mismatch — invoice-level, OUTSIDE the
        # per-line jurisdiction_ambiguous guard so it is not suppressed under
        # AMBIGUOUS. At the terminal gate tax_jurisdiction is reliably populated.
        resolved = (state.get(TAX_JURISDICTION_KEY) or "").strip().upper()
        if inv.currency == "SGD" and resolved == "MALAYSIA":
            reasons.append(f"{label}: MY-jurisdiction but currency=SGD (currency_mismatch)")
        for line in inv.lines:
            if jurisdiction_ambiguous:
                continue
            if line.account_flagged:
                reasons.append(
                    f"{label}: line '{line.description}' flagged for account review"
                    + (f" ({line.account_flag_reason})" if line.account_flag_reason else "")
                )
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


def _preview_export_unmapped(state: dict) -> dict:
    """Build export rows early (pre-route) to flag blank tax/creditor codes."""
    doc_type = (state.get(DOC_TYPE_KEY) or "").strip().lower()
    if doc_type == "bank_statement":
        return {"count": 0, "details": []}

    software = ""
    software_flagged = False
    if "software" in state:
        software, software_flagged = _resolve_software_from_state(state)
    if not software or software_flagged:
        return {"count": 0, "details": []}

    _rates = state.get(JURISDICTION_RATES_KEY) or {}
    _ref_yaml = _rates.get("reference_yaml") or state.get(TAX_JURISDICTION_KEY) or None
    client_region, _ = _client_region_and_currency_from_state(state)
    clf_res = resolve_tax_classifier_reference(
        _ref_yaml,
        client_region=client_region,
    )
    if clf_res.flagged or clf_res.value is None:
        return {"count": 0, "details": []}

    exporter = get_exporter(software, classifier=clf_res.value)
    if hasattr(exporter, "configure_client_context"):
        exporter.configure_client_context(
            tax_codes=tax_codes_from_state(state),
            entity_memory=entity_memory_from_state(state),
            coa_keys=coa_keys_from_state(state),
        )

    direction = state.get(DIRECTION_KEY) or "purchase"
    default_sheet = "Sales" if direction == "sales" else "Purchase"
    batches: list[dict] = []
    for inv in _normalized_from_state(state):
        row_doc_type = "sales" if default_sheet == "Sales" else "purchase"
        rows = exporter.rows([inv], row_doc_type)
        batches.append({"sheet": default_sheet, "rows": rows})
    return collect_export_unmapped_summary(batches, exporter)


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


def _approval_summary(reasons: list[str], *, export_unmapped: dict | None = None) -> str:
    """Human-readable summary of why the document needs approval."""
    hard, soft = partition_and_group_reasons(reasons)
    display_lines = [item.message for item in hard] + [item.message for item in soft]
    header = (
        "Please review the proposed accounting entries — the following need a "
        "human decision before they are added to the ledger:"
    )
    bullets = "\n".join(f"  • {line}" for line in display_lines)
    summary = f"{header}\n{bullets}"
    unmapped_note = format_unmapped_export_note(export_unmapped)
    if unmapped_note:
        summary = f"{summary}\n\n  • {unmapped_note}"
    return summary


def _read_preview_from_state(state: dict, *, max_fields: int = 8) -> str:
    """Compact Phase 1 labeled_fields preview for the approval card."""
    raw = state.get(DOCUMENT_RECORDS_KEY) or []
    if not raw:
        return ""
    lines: list[str] = ["*What was read from the document:*"]
    for idx, item in enumerate(raw[:3]):
        prefix = f"Doc {idx + 1}: " if len(raw) > 1 else ""
        shown = 0
        if isinstance(item, dict):
            labeled_fields = item.get("labeled_fields") or []
            annotations = item.get("annotations") or []
        else:
            labeled_fields = item.labeled_fields
            annotations = item.annotations
        for field in labeled_fields:
            label = field.get("label") if isinstance(field, dict) else field.label
            value = field.get("value") if isinstance(field, dict) else field.value
            if shown >= max_fields:
                lines.append(f"{prefix}… ({len(labeled_fields) - shown} more fields)")
                break
            lines.append(f"{prefix}{label}: {value}")
            shown += 1
        if annotations and shown < max_fields:
            for ann in annotations[:2]:
                text = ann.get("text") if isinstance(ann, dict) else ann.text
                lines.append(f"{prefix}📝 {text}")
    return "\n".join(lines)


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
    invoices = _normalized_from_state(ctx.state)
    needs_review, reasons = _needs_review(ctx.state)
    is_multi = len(invoices) > 1

    # WS4: reconcile-only at the terminal gate → one totals re-read before HITL.
    if (
        needs_review
        and not is_multi
        and _is_reconcile_only_needs_review_reasons(reasons)
        and not ctx.state.get(RECONCILE_REEXTRACT_ATTEMPTED_KEY)
    ):
        pdf_bytes, mime_type = await _load_pdf_bytes(ctx)
        _reextract_with_hint(ctx, RECONCILE_REREAD_HINT, pdf_bytes, mime_type)
        ctx.state[RECONCILE_REEXTRACT_ATTEMPTED_KEY] = True
        needs_review, reasons = _needs_review(ctx.state)

    if not needs_review:
        ctx.state[APPROVAL_STATUS_KEY] = "auto_approved"
        return

    export_unmapped = _preview_export_unmapped(ctx.state)
    ctx.state["export_unmapped_summary"] = export_unmapped

    summary = _approval_summary(
        reasons,
        export_unmapped=export_unmapped,
    )
    read_preview = _read_preview_from_state(ctx.state)
    if read_preview:
        summary = f"{read_preview}\n\n{summary}"
    # Stash the summary in state so the (Slack-owning) runner can render the
    # approval card text without re-deriving it; the node stays Slack-agnostic.
    ctx.state["approval_message"] = summary
    yield RequestInput(
        interrupt_id=_approval_interrupt_id(ctx.state),
        message=summary,
        response_schema=ApproveDecision,
    )


def _doc_key(state: dict, sheet: str, identity: str, index: int, *, period: str = "", page_range: tuple[int, int] | None = None) -> str:
    """Content-based per-document dedupe key (re-uploading re-emits the same key).

    Does NOT include the Slack file id (which changes on every upload).
    Bank statements include the statement period so different months are
    distinct; invoices use reference + optional page_range (WS-5.4).
    """
    if period:
        ident = (identity or "").strip() or f"i{index}"
        return f"{sheet}:{ident}:{period}"
    return ledger_doc_identity(sheet, identity, page_range, index=index)


@node
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
    software = ""
    software_flagged = False
    if "software" in state:
        software, software_flagged = _resolve_software_from_state(state)

    # Representative FY for the run (the workbook is per-FY); take the first route.
    fy = str(routes[0]["fy"]) if routes else "unknown"

    batches: list[dict] = []
    export_unmapped: dict = {}
    import_readiness: dict = {}
    account_flagged_summary: dict = {}
    invoices: list = []

    if doc_type == "bank_statement":
        kind = "bank"
        exporter = get_bank_exporter()
        statements = _bank_statements_from_state(state)
        client_region, client_currency = _client_region_and_currency_from_state(state)
        for idx, (stmt, route) in enumerate(zip(statements, routes)):
            ccy_res = resolve_currency(
                stmt.currency,
                client_region=client_region,
                client_currency=client_currency,
            )
            currency = ccy_res.value
            sheet = bank_sheet_title(
                bank_name=stmt.bank_name,
                account_number=stmt.account_number,
                currency=currency,
            )
            rows = exporter.bank_rows(stmt)
            ident = f"{stmt.account_number or stmt.bank_name}:{currency or '?'}"
            batches.append(
                {
                    "sheet": sheet,
                    "doc_key": _doc_key(state, sheet, ident, idx,
                                       period=stmt.statement_period or ""),
                    "rows": rows,
                }
            )
    else:
        kind = "invoice"
        invoices = _normalized_from_state(state)
        _rates = state.get(JURISDICTION_RATES_KEY) or {}
        _ref_yaml = _rates.get("reference_yaml") or state.get(TAX_JURISDICTION_KEY) or None
        client_region, _ = _client_region_and_currency_from_state(state)
        clf_res = resolve_tax_classifier_reference(
            _ref_yaml,
            client_region=client_region,
        )
        if (
            software
            and not software_flagged
            and not clf_res.flagged
            and clf_res.value is not None
        ):
            exporter = get_exporter(software, classifier=clf_res.value)
            if hasattr(exporter, "configure_client_context"):
                exporter.configure_client_context(
                    tax_codes=tax_codes_from_state(state),
                    entity_memory=entity_memory_from_state(state),
                    coa_keys=coa_keys_from_state(state),
                )
            for idx, (inv, route) in enumerate(zip(invoices, routes)):
                sheet = route.get("sheet") or "Purchase"
                row_doc_type = "sales" if sheet == "Sales" else "purchase"
                rows = exporter.rows([inv], row_doc_type)
                batches.append(
                    {
                        "sheet": sheet,
                        "doc_key": _doc_key(
                            state,
                            sheet,
                            inv.invoice_number,
                            idx,
                            page_range=inv.page_range,
                        ),
                        "rows": rows,
                    }
                )
            export_unmapped = collect_export_unmapped_summary(batches, exporter)
            state["export_unmapped_summary"] = export_unmapped
            account_flagged_summary = collect_account_flagged_summary(batches)
            state["account_flagged_summary"] = account_flagged_summary
            import_readiness = collect_import_readiness(
                batches, exporter, unmapped=export_unmapped
            )
            state["import_readiness"] = import_readiness
        elif software_flagged:
            logger.warning(
                "consolidate_node: unresolved software %r — skipping export rows",
                state.get("software"),
            )

    payload = {
        "client_id": client_id,
        "client_name": state.get("client_name") or "",
        "fy": fy,
        "kind": kind,
        "software": software,
        "doc_type": doc_type,
        # Lever 3 (ADR-0017 §2): carry the classifier's free-text label so the
        # confident-path note can read "delivery order" instead of "document"
        # when doc_type is the generic "other".  None for recognised enum types.
        "free_type": state.get(CLASSIFY_FREE_TYPE_KEY),
        # ``delivered`` is set True by deliver_node on the clean no-pause path.
        # It is NOT present when the HITL-approve path calls persist_and_deliver
        # directly (deliver_node never ran).  Used to gate the confident-path note.
        "delivered": bool(state.get("delivered")),
        "batches": batches,
        "export_unmapped_summary": export_unmapped if kind == "invoice" else {},
        "import_readiness": import_readiness if kind == "invoice" else {},
        "account_flagged_summary": account_flagged_summary if kind == "invoice" else {},
    }
    if kind == "invoice":
        payload["extracted_doc_count"] = len(invoices)
        page_count = state.get("input_page_count")
        if page_count is not None:
            payload["input_page_count"] = int(page_count)
        partial = state.get("partial_failure_warnings")
        if partial:
            payload["partial_failure_warnings"] = list(partial)
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


def compose_delivery_summary(payload: dict) -> str:
    """Compose the user-facing delivery summary from a LEDGER_ROWS_KEY payload.

    Pure function used by both ``deliver_node`` (clean path) and
    ``persist_and_deliver`` (HITL-approve path) so the two paths stay in
    delivery-card lockstep. If we ever drift again, it's because someone
    bypassed this helper.
    """
    batches = payload.get("batches") or []
    fy = payload.get("fy", "?")
    kind = payload.get("kind", "document")
    client_name = (payload.get("client_name") or "").strip()

    doc_label = "Bank Statement" if kind == "bank" else "Ledger"
    prefix = f"{client_name} – " if client_name else ""
    sw = software_label(str(payload.get("software") or ""), empty_label="")
    sw_suffix = f" ({sw})" if sw and kind != "bank" else ""
    destination = f"**{prefix}{doc_label} FY{fy}{sw_suffix}**"

    if not batches:
        return "No entries were produced for this document."

    if kind == "bank":
        parts: list[str] = []
        for batch in batches:
            rows = batch.get("rows") or []
            txn_rows = [
                r for r in rows
                if (r.get("Description") or "") not in ("BALANCE B/F", "TOTALS")
            ]
            txn_count = len(txn_rows)
            month_label = _month_label(txn_rows)
            closing = _closing_balance_from_rows(rows)
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
        return f"📒 Added {'; '.join(parts)} to your {destination}."

    n_rows = sum(len(b.get("rows") or []) for b in batches)
    return (
        f"📒 Added {n_rows} line{'s' if n_rows != 1 else ''} from "
        f"{len(batches)} document{'s' if len(batches) != 1 else ''} "
        f"to your {destination}."
    )


def compose_confident_note(
    payload: dict,
    *,
    doc_type: str,
    free_type: Optional[str] = None,
) -> str:
    """Compose a concise plain-language note for confident (no-pause) deliveries.

    Reads the same ``LEDGER_ROWS_KEY`` payload that ``compose_delivery_summary``
    uses and returns a one-liner like:
        "Posted this expense claim — 3 lines, reconciles to $240.00, coded to Travel."

    Handles missing pieces gracefully:
    - No batches / rows → short fallback note.
    - No account code → omits the "coded to" clause.
    - When ``doc_type`` is the generic ``"other"`` and ``free_type`` is provided,
      the note uses the free_type label (e.g. "delivery_order" → "delivery order")
      so the user sees "Posted this delivery order — …" instead of the opaque
      "Posted this document — …".  For known enum types ``free_type`` is ignored.
    """
    batches = payload.get("batches") or []

    # Human-readable label for the doc type.
    # Lever 3: when doc_type is the generic "other" but free_type provides a
    # more specific label, use it (underscores → spaces).  Empty string free_type
    # is treated as absent (fall back to "document").
    _doc_labels: dict[str, str] = {
        "expense_claim": "expense claim",
        "invoice": "invoice",
        "receipt": "receipt",
        "credit_note": "credit note",
        "bank_statement": "bank statement",
        "statement_of_account": "statement of account",
        "other": "document",
    }
    if doc_type == "other" and free_type:
        doc_label = free_type.replace("_", " ")
    else:
        doc_label = _doc_labels.get(doc_type, doc_type.replace("_", " "))

    if not batches:
        return f"Posted this {doc_label}."

    # Count total non-structural rows across all invoice batches.
    all_rows: list[dict] = []
    for batch in batches:
        all_rows.extend(batch.get("rows") or [])

    n_lines = len(all_rows)

    # Derive reconcile total from rows (sum of net amounts).
    # Prefer the payload-level currency (set by consolidate_node from the statement)
    # before falling back to individual row values, then "SGD" as last resort.
    #
    # Profile-aware: use ``exporter.column_for_field(...)`` to look up the ACTUAL
    # column that carries the line net ("sub_total") and the currency, instead of
    # guessing literal header strings. Each exporter (QBS / Xero / AutoCount /
    # SQL Account) declares its own column→logical mapping, so a single call
    # works for every software. If a field has no column for this doc_type
    # (e.g. AutoCount purchase has no currency column; Xero has no per-line
    # sub_total), the helper returns None and we skip that piece of the note
    # rather than rendering a blank or wrong value.
    from collections import Counter

    sw_res = resolve_software(str(payload.get("software") or ""))
    _sub_total_col: Optional[str] = None
    _currency_col: Optional[str] = None
    _account_col: Optional[str] = None
    _exp = None
    if not sw_res.flagged and sw_res.value:
        try:
            _exp = get_exporter(sw_res.value)
        except Exception:
            _exp = None
    if _exp is not None:
        _sub_total_col = _exp.column_for_field("sub_total", doc_type)
        _currency_col = _exp.column_for_field("currency", doc_type)
        _account_col = _exp.column_for_field("account_code", doc_type)

    client_region, client_currency = _client_region_and_currency_from_state(
        {"region": payload.get("region"), "base_currency": payload.get("base_currency")}
        if payload.get("region") or payload.get("base_currency")
        else {}
    )
    total: Optional[float] = None
    currency_res = resolve_currency(
        payload.get("currency"),
        client_region=client_region,
        client_currency=client_currency,
    )
    currency = currency_res.value
    for row in all_rows:
        if _sub_total_col:
            amt = row.get(_sub_total_col)
            if amt is not None:
                try:
                    total = (total or 0.0) + float(amt)
                except (TypeError, ValueError):
                    pass
        if _currency_col:
            row_currency = row.get(_currency_col)
            if row_currency:
                currency = row_currency

    # Derive dominant account code (most frequent non-empty value).
    codes: list[str] = []
    if _account_col:
        codes = [row.get(_account_col) for row in all_rows if row.get(_account_col)]
    dominant_code: Optional[str] = None
    if codes:
        dominant_code = Counter(codes).most_common(1)[0][0]

    # Build the note.
    line_str = f"{n_lines} line{'s' if n_lines != 1 else ''}"
    total_str = (
        f"reconciles to {currency} {total:,.2f}" if total is not None else ""
    )
    coded_str = f"coded to {dominant_code}" if dominant_code else ""

    parts = [p for p in [total_str, coded_str] if p]
    detail = " — " + ", ".join(parts) if parts else ""

    note = f"Posted this {doc_label} — {line_str}{detail}."
    unmapped_note = format_unmapped_export_note(payload.get("export_unmapped_summary"))
    if unmapped_note:
        note = f"{note} {unmapped_note}."
    readiness_note = format_import_readiness_note(payload.get("import_readiness"))
    if readiness_note:
        note = f"{note} {readiness_note}"
    flagged_note = format_account_flagged_note(
        payload.get("account_flagged_summary")
        or collect_account_flagged_summary(batches)
    )
    if flagged_note and not readiness_note:
        note = f"{note} {flagged_note}"
    return note


@node
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
    summary = compose_delivery_summary(payload)
    state[DELIVER_SUMMARY_KEY] = summary
    state["delivered"] = True
    return Event(output={"delivered": True, "summary": summary})


# --------------------------------------------------------------------------- #
# Serialization helpers — NormalizedInvoice / BankStatement <-> plain dict
# (workflow state must be JSON-serializable basic types)
#
# The real implementations live in `.normalized_invoice_codec`. The shims
# below keep call sites unchanged (tests still use ``nodes._inv_to_dict`` /
# ``nodes._dict_to_inv``) while routing every round-trip through the codec
# so ``tax_visible_on_document`` and ``direction_reason`` are preserved.
# --------------------------------------------------------------------------- #


def _inv_to_dict(inv: NormalizedInvoice) -> dict:
    return invoice_to_dict(inv)


def _dict_to_inv(d: dict) -> NormalizedInvoice:
    return dict_to_invoice(d)


def _normalized_from_state(state: dict) -> list[NormalizedInvoice]:
    return [dict_to_invoice(d) for d in (state.get(NORMALIZED_KEY) or [])]


def _bank_to_dict(s: BankStatement) -> dict:
    return bank_to_dict(s)


def _bank_from_state(state: dict) -> list[dict]:
    return list(state.get(BANK_STATEMENTS_KEY) or [])


def _dict_to_bank(d: dict) -> BankStatement:
    return dict_to_bank(d)


def _bank_statements_from_state(state: dict) -> list[BankStatement]:
    return [dict_to_bank(d) for d in (state.get(BANK_STATEMENTS_KEY) or [])]


def _parse_statement_period_anchor(period: Optional[str]) -> Optional[date]:
    """Parse the opening date from a printed statement period string."""
    if not period or not str(period).strip():
        return None
    import re
    from datetime import datetime

    text = str(period).strip()
    if re.search(r"\s+to\s+", text, flags=re.I):
        head = re.split(r"\s+to\s+", text, maxsplit=1, flags=re.I)[0].strip()
    elif " - " in text:
        head = text.split(" - ", 1)[0].strip()
    else:
        head = text
    for fmt in (
        "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%B-%Y",
        "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    return _parse_iso(head)


def _bank_run_representative_date(state: dict) -> date:
    """Pick one routing date for the whole bank PDF.

  Scans every currency section for transaction dates, then falls back to
  ``statement_period`` on any account. Only uses ``date.today()`` when the
  entire extraction is dateless — never because the first-listed currency
  (often CNH) happened to have zero transactions.
    """
    statements = _bank_from_state(state)
    best: Optional[date] = None
    for s in statements:
        for t in s.get("transactions") or []:
            d = _parse_iso(t.get("date"))
            if d is not None and (best is None or d > best):
                best = d
    if best is not None:
        return best
    for s in statements:
        anchor = _parse_statement_period_anchor(s.get("statement_period"))
        if anchor is not None:
            return anchor
    return date.today()


def _bank_dict_representative_date(s: dict) -> date:
    best: Optional[date] = None
    for t in s.get("transactions") or []:
        d = _parse_iso(t.get("date"))
        if d is not None and (best is None or d > best):
            best = d
    if best is not None:
        return best
    anchor = _parse_statement_period_anchor(s.get("statement_period"))
    return anchor if anchor is not None else date.today()


def _route_to_dict(r: DocRoute) -> dict:
    return {
        "fy": r.fy,
        "bucket": r.bucket,
        "workbook": r.workbook,
        "sheet": r.sheet,
        "archive_path": r.archive_path,
    }
