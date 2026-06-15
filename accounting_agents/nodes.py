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
    # Persist the classifier's confidence so the extract reviewer's
    # ``low_classify_confidence`` signal (#5) can read it cheaply downstream.
    ctx.state[CLASSIFY_CONFIDENCE_KEY] = cls.confidence

    if doc_type == "bank_statement":
        ctx.state[DOC_TYPE_KEY] = "bank_statement"
        ctx.state[DIRECTION_KEY] = None
        return Event(route=ROUTE_BANK, output={"doc_type": "bank_statement"})

    direction = DIRECTION_FN(
        cls,
        client_name=ctx.state.get("client_name"),
        client_uen=ctx.state.get("client_uen"),
    )
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
    # A re_extract request (chat tool / ADR-0010) seeds ``review_hint`` into run
    # state so the FIRST read is steered too — not just the reviewer's retry. Only
    # pass the kwarg when a hint is actually present so the normal file-drop path
    # (no hint) calls ``EXTRACT_BUNDLE_FN`` exactly as before.
    review_hint = (ctx.state.get("review_hint") or "").strip()
    if review_hint:
        bundle: ExtractedInvoiceBundle = EXTRACT_BUNDLE_FN(
            data, mime_type, model=MODEL_LITE, hint=review_hint
        )
    else:
        bundle = EXTRACT_BUNDLE_FN(data, mime_type, model=MODEL_LITE)

    normalized = _normalize_bundle(ctx, bundle)
    ctx.state[NORMALIZED_KEY] = _guard_state_payload(
        NORMALIZED_KEY, [_inv_to_dict(i) for i in normalized]
    )
    return Event(output={"count": len(normalized)})


def _normalize_bundle(ctx, bundle: ExtractedInvoiceBundle) -> list[NormalizedInvoice]:
    """Reconcile + normalize every invoice in ``bundle`` into NormalizedInvoices.

    Shared by ``extract_invoice_node`` (first pass) and the extract reviewer's
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
    our_gst = bool(ctx.state.get("tax_registered", True))
    base_currency: str = ctx.state.get("base_currency") or "SGD"

    normalized: list[NormalizedInvoice] = []
    for ex in bundle.invoices:
        ok, _detail = reconcile(ex)
        inv = to_normalized(
            ex,
            direction=effective_direction,
            our_gst_registered=our_gst,
            base_currency=base_currency,
            fx_rate=ex.fx_rate,
        )
        # to_normalized already sets reconciled=False when needs_fx_review is True;
        # only overwrite with the totals-reconcile result when FX has not already
        # forced reconciled=False, so we don't accidentally clear the FX flag.
        if not inv.needs_fx_review:
            inv.reconciled = ok
        # Self-referential / ambiguous direction guard (mirrors pipeline.py).
        if direction == "self_referential":
            inv.reconciled = False
            review_note = (
                "needs review: self-referential document — issuer and bill-to "
                "both match client; not booked as a purchase"
            )
            inv.reconcile_note = (
                f"{inv.reconcile_note}; {review_note}"
                if inv.reconcile_note
                else review_note
            )
        elif direction == "unknown":
            inv.reconciled = False
            review_note = (
                "needs review: direction unknown — could not determine whether "
                "client is issuer or bill-to; defaulted to purchase for routing"
            )
            inv.reconcile_note = (
                f"{inv.reconcile_note}; {review_note}"
                if inv.reconcile_note
                else review_note
            )
        normalized.append(inv)
    return normalized


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
        )

    ctx.state[NORMALIZED_KEY] = _guard_state_payload(
        NORMALIZED_KEY, [_inv_to_dict(i) for i in invoices]
    )
    return Event(output={"count": len(invoices)})


async def tax_node(ctx) -> Event:
    """Classify SG GST treatment per line, per normalized invoice."""
    invoices = _normalized_from_state(ctx.state)
    clf = TaxClassifier()
    for inv in invoices:
        for line in inv.lines:
            clf.classify_line(line, inv)

    ctx.state[NORMALIZED_KEY] = _guard_state_payload(
        NORMALIZED_KEY, [_inv_to_dict(i) for i in invoices]
    )
    return Event(output={"count": len(invoices)})


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

    # Signal #1: bundle_empty — extraction produced zero invoices.
    if not invoices:
        reasons.append("bundle_empty")

    # Signal #4: doc_type_other — classifier could not place the document.
    doc_type = (state.get(DOC_TYPE_KEY) or "").strip().lower()
    if doc_type == "other":
        reasons.append("doc_type_other")

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
            reasons.append(f"unreconciled: {label} ({note})")

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

    instruction = (
        "You are a meticulous bookkeeping QA reviewer. An automated reader "
        "extracted an invoice/receipt and a deterministic checker flagged these "
        "concerns:\n"
        + "\n".join(f"- {r}" for r in reasons)
        + "\n\nDecide ONE verdict and ALWAYS return it explicitly — never end "
        "with only a tool call and never reply with empty text:\n"
        f"- '{REVIEW_VERDICT_OK}': the extraction is acceptable as-is.\n"
        f"- '{REVIEW_VERDICT_HINTS}': a re-extraction with a specific hint would "
        "likely fix it; provide a short 'hint'.\n"
        f"- '{REVIEW_VERDICT_CLARIFY}': only a human can resolve it; provide a "
        "short 'question' for the accountant.\n"
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
            contents=[instruction, json.dumps(state.get(NORMALIZED_KEY) or [], default=str)],
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


def _run_reviewer_loop(ctx, reasons: list[str], pdf_bytes: bytes) -> str:
    """Bounded IN-NODE reviewer loop (NOT a graph cycle). Returns a verdict.

    §9.3 ceiling: at most ``REVIEW_MAX_REVIEWS`` critic calls +
    ``REVIEW_MAX_REEXTRACTS`` re-extract. ``hints_needed`` (within the re-extract
    cap) re-runs ``EXTRACT_BUNDLE_FN`` with the hint appended + ``_normalize_bundle``,
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
            _reextract_with_hint(ctx, hint, pdf_bytes)
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


def _reextract_with_hint(ctx, hint: str, pdf_bytes: bytes) -> None:
    """Re-run extraction with ``hint`` appended, re-normalize, rewrite state.

    Reuses ``_normalize_bundle`` (shared with ``extract_invoice_node``) so the
    re-extract path never duplicates normalization logic. ``review_hint`` is
    stored for audit; the real extractor receives ``hint`` as a kwarg.
    """
    ctx.state["review_hint"] = hint
    bundle: ExtractedInvoiceBundle = EXTRACT_BUNDLE_FN(
        pdf_bytes, "application/pdf", model=MODEL_LITE, hint=hint,
    )
    normalized = _normalize_bundle(ctx, bundle)
    ctx.state[NORMALIZED_KEY] = _guard_state_payload(
        NORMALIZED_KEY, [_inv_to_dict(i) for i in normalized]
    )


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

    # Tripped: the bounded loop may re-extract, so load the source PDF now.
    pdf_bytes, _mime = await _load_pdf_bytes(ctx)
    verdict = _run_reviewer_loop(ctx, reasons, pdf_bytes)
    ctx.state[REVIEW_VERDICT_KEY] = verdict

    if verdict != REVIEW_VERDICT_CLARIFY:
        return

    # Circuit-break to the human mid-flow (§9.5). Distinct ``:review`` interrupt
    # id keeps this pause separate from the terminal ``approval_gate`` pause.
    yield RequestInput(
        interrupt_id=review_interrupt_id,
        message=ctx.state.get("review_question") or _review_clarify_question(reasons),
        response_schema=ReviewClarifyDecision,
    )


async def _maybe_load_pdf(ctx, decision) -> bytes:
    """Load the source PDF bytes only when the resume action is a re-extract.

    ``reject`` / ``confirm_as_is`` never re-read the artifact, so resuming those
    must not require an artifact service to be present.
    """
    data = decision if isinstance(decision, dict) else {}
    if data.get("action") == "reextract_as":
        pdf_bytes, _mime = await _load_pdf_bytes(ctx)
        return pdf_bytes
    return b""


def _apply_review_clarify(ctx, decision, pdf_bytes: bytes) -> None:
    """Apply the human's ReviewClarifyDecision resume payload to the run state."""
    data = decision if isinstance(decision, dict) else {}
    action = data.get("action")
    ctx.state["review_clarify_action"] = action

    if action == "reject":
        ctx.state[NORMALIZED_KEY] = []
        return
    if action == "reextract_as":
        _reextract_with_hint(ctx, data.get("hint") or "", pdf_bytes)
        return
    # confirm_as_is (or missing/unknown action): wave the current extraction
    # through unchanged.


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
    ctx.state[BANK_STATEMENTS_KEY] = _guard_state_payload(
        BANK_STATEMENTS_KEY, [_bank_to_dict(s) for s in statements]
    )
    return Event(output={"count": len(statements)})


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
            ctx.state[NORMALIZED_KEY] = _guard_state_payload(NORMALIZED_KEY, invoices)
    return Event(output={"decision": choice})


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
    invoices = _normalized_from_state(ctx.state)
    needs_review, reasons = _needs_review(ctx.state)
    is_multi = len(invoices) > 1

    if not needs_review and not is_multi:
        ctx.state[APPROVAL_STATUS_KEY] = "auto_approved"
        return

    # Multi-entity bundles get a bundle-level reason when no per-doc flags fired.
    if is_multi and not reasons:
        total_lines = sum(len(getattr(inv, "lines", [])) for inv in invoices)
        reasons = [
            f"{len(invoices)} sub-documents extracted, {total_lines} total lines"
            " — review before posting."
        ]

    summary = _approval_summary(reasons)
    # Stash the summary in state so the (Slack-owning) runner can render the
    # approval card text without re-deriving it; the node stays Slack-agnostic.
    ctx.state["approval_message"] = summary
    yield RequestInput(
        interrupt_id=_approval_interrupt_id(ctx.state),
        message=summary,
        response_schema=ApproveDecision,
    )


def _doc_key(state: dict, sheet: str, identity: str, index: int, *, period: str = "") -> str:
    """Content-based per-document dedupe key (re-uploading re-emits the same key).

    Does NOT include the Slack file id (which changes on every upload).
    Bank statements include the statement period so different months are
    distinct; invoices use the invoice number.
    """
    ident = (identity or "").strip() or f"i{index}"
    if period:
        return f"{sheet}:{ident}:{period}"
    return f"{sheet}:{ident}"


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
    software = state.get("software")  # seeded by the runner; get_exporter raises if missing

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
                    "doc_key": _doc_key(state, sheet, stmt.account_number or stmt.bank_name, idx,
                                       period=stmt.statement_period or ""),
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
        "client_name": state.get("client_name") or "",
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
    client_name = (payload.get("client_name") or "").strip()

    # Destination workbook name — bank statements are NOT ledgers, so name them
    # accordingly and never call a bank doc a "ledger". Mirrors the file naming in
    # ledger_store.append_rows (``<Client> - BankStatement_FY<fy>`` / ``Ledger_FY<fy>``).
    doc_label = "Bank Statement" if kind == "bank" else "Ledger"
    prefix = f"{client_name} – " if client_name else ""
    destination = f"**{prefix}{doc_label} FY{fy}**"

    if not batches:
        state[DELIVER_SUMMARY_KEY] = "No entries were produced for this document."
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

        summary = f"📒 Added {'; '.join(parts)} to your {destination}."
    else:
        n_rows = sum(len(b.get("rows") or []) for b in batches)
        summary = (
            f"📒 Added {n_rows} line{'s' if n_rows != 1 else ''} from "
            f"{len(batches)} document{'s' if len(batches) != 1 else ''} "
            f"to your {destination}."
        )

    state[DELIVER_SUMMARY_KEY] = summary
    state["delivered"] = True
    return Event(output={"delivered": True, "summary": summary})


# --------------------------------------------------------------------------- #
# Serialization helpers — NormalizedInvoice / BankStatement <-> plain dict
# (workflow state must be JSON-serializable basic types)
# --------------------------------------------------------------------------- #
from dataclasses import asdict  # noqa: E402


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
        doc_subtotal=d.get("doc_subtotal"),
        doc_gst_total=d.get("doc_gst_total"),
        doc_total=d.get("doc_total"),
        our_gst_registered=bool(d.get("our_gst_registered", True)),
        fx_rate=d.get("fx_rate"),
        original_total=d.get("original_total"),
        original_currency=d.get("original_currency"),
        needs_fx_review=bool(d.get("needs_fx_review", False)),
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
