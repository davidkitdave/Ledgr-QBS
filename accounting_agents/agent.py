"""ADK 2.0 graph for the Ledgr accounting agent system.

Top-level shape (verified against google-adk 2.2.0)::

    START
      -> coordinator        (LlmAgent, output_schema=RouteDecision,
                             before_agent_callback loads the channel's profile)
      -> dynamic_router     (@node -> Event(route=...))
      -> { "document": DocumentWorkflow,
           "question": help_node,   # defensive fallback — text now bypasses
                                    # the graph and runs on assistant_app (ADR-0008)
           "unknown":  help_node }

The chat lane runs OUTSIDE this graph on ``assistant_app`` — a standalone root
``LlmAgent`` (multi-turn, sees per-thread session history). See
``docs/adr/0008-chat-lane-standalone-root-agent.md``.

DocumentWorkflow (resumable, declarative sequential — Track A, Phase 6)::

    START
      -> classify_node
      -> { "commercial_doc": pipeline_commercial
                              (START -> extract_invoice -> review_extraction
                               -> categorize -> resolve_jurisdiction -> tax
                               -> approval_gate -> apply_decision_node
                               -> route_node -> consolidate_node -> deliver_node),
           "bank_statement": pipeline_bank
                              (START -> extract_bank
                               -> approval_gate -> apply_decision_node
                               -> route_node -> consolidate_node -> deliver_node) }

The pipeline is a static ``Workflow(edges=[...])`` so ADK web renders it as a
left→right chain (Track A). Each lane pipeline is built from
:mod:`accounting_agents.lane_config.DOC_TYPE_TO_LANE` — the single declarative
source of truth that ``classify_node`` also reads for its ``Event.route``
label. Adding a new doc type is one entry in the lane registry.

``document_workflow_driver`` is retained as a parallel imperative driver for
behaviour tests (``tests/test_graph_wiring.py``); it is NOT used by any App.
Both paths share the same node functions, so behaviour is identical.

HITL is preserved unchanged because interrupt correlation is by ``interrupt_id``
string (independent of scheduling):
- Mid-flow ``:review`` pause: ``review_extraction_node`` is itself
  ``rerun_on_resume=True`` and reads its decision from ``ctx.resume_inputs`` —
  the scheduler simply re-runs it on resume and it applies its own decision.
- Terminal pause: ``approval_gate`` is a default node, so on resume ADK's replay
  interceptor completes it with the human's ``ApproveDecision`` as its OUTPUT
  (``_replay_interceptor.check_interception`` Case 4 — ``rerun_on_resume=False``
  → resolved response becomes the node output), which the static edge threads
  into ``apply_decision_node`` as its ``node_input``.

API facts grounded in the installed 2.2.0 source (see the task report for detail):
- ``Workflow(name=..., edges=[...])``; chains are tuples ``(START, a, b)``; a
  conditional fan-out is a dict element ``{"route": node_or_(tuple)}``; a node
  reached by multiple edges is the convergence point.
- ``ctx.run_node(fn, node_input=...)`` runs a node dynamically and returns its
  output; the calling node MUST be ``@node(rerun_on_resume=True)``.
- ``App(root_agent=<BaseNode|BaseAgent>, name=..., resumability_config=...)``;
  ``ResumabilityConfig(is_resumable=True)``.
- ``LlmAgent(mode="single_turn")`` is REQUIRED for agents used as graph nodes
  (the scheduler rejects ``mode="chat"`` agents reached from a preceding node);
  ``output_schema`` makes the agent emit a structured object that flows to the
  next node as ``node_input``.
- ``before_agent_callback(callback_context: Context) -> Optional[types.Content]``;
  ``CallbackContext`` is an alias of ``Context``; ``ctx.state`` is the mutable
  session State. The parked ``make_load_client_by_channel_callback`` is
  duck-typed on ``.state`` and returns ``None`` — compatible as-is.

Session ``user_id`` convention: the Slack layer runs each channel as its own ADK
session with ``user_id == session_id == channel_id`` (channel = client = session
scope). The profile callback reads ``state["channel_id"]`` to resolve the client.
"""

from __future__ import annotations

from typing import Literal

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.workflow import START, Edge, Workflow, node
from pydantic import BaseModel, Field

from invoice_processing.export.client_context import (
    ClientContext,
    FirestoreClientStore,
    make_load_client_by_channel_callback,
)

from . import config  # ensures AI Studio env is set before any ADK model init
from . import nodes
from .assistant import assistant_agent  # noqa: F401 — re-exported via __all__
from .plugins.ledgr_reflect_retry import LedgrReflectRetryPlugin

# --------------------------------------------------------------------------- #
# Route labels (top-level coordinator router)
# --------------------------------------------------------------------------- #

ROUTE_DOCUMENT = "document"
ROUTE_QUESTION = "question"
ROUTE_UNKNOWN = "unknown"


class RouteDecision(BaseModel):
    """Structured classification of a single inbound Slack turn."""

    intent: Literal["document", "question", "unknown"] = Field(
        description=(
            "Classify the user's turn: 'document' when a file (invoice, receipt, "
            "or bank statement) was uploaded to be processed; 'question' when the "
            "user is asking about their ledger or accounting data; 'unknown' when "
            "the intent is unclear."
        )
    )


# --------------------------------------------------------------------------- #
# Profile-loading callback (adapted parked function)
#
# The parked make_load_client_by_channel_callback was written for adk 1.30 but is
# duck-typed: its inner callback only touches ``callback_context.state`` (.get /
# item-set) and returns None. The 2.2.0 BeforeAgentCallback signature is
# ``(CallbackContext) -> Optional[types.Content]`` and CallbackContext aliases
# Context, whose ``.state`` is exactly that mapping — so the parked function is
# already signature-compatible. We wrap it in a thin, defensive adapter so the
# graph never aborts on a loader error and so the 2.2.0 typing is explicit at the
# call site, WITHOUT modifying (or breaking the tests of) the parked function.
# --------------------------------------------------------------------------- #

_load_client_by_channel = make_load_client_by_channel_callback(FirestoreClientStore())


def _playground_default_context() -> ClientContext:
    """Build the synthetic :class:`ClientContext` used when no real profile loads.

    In dev / playground mode, the ``load_client_profile`` callback injects a
    default client profile so the document lane can run end-to-end without a
    real Slack channel. To make the playground useful for testing real
    invoices, the defaults can be overridden by env vars (preferred for quick
    tweaks) or by a local ``playground_profile.json`` file dropped in the
    workspace root (preferred for richer profiles). All values are optional —
    missing ones fall back to the hard-coded defaults.

    Environment variables (all optional)::

        LEDGR_PLAYGROUND_CLIENT_ID      default: "playground"
        LEDGR_PLAYGROUND_CLIENT_NAME    default: "Playground Client"
        LEDGR_PLAYGROUND_CLIENT_UEN     default: ""
        LEDGR_PLAYGROUND_REGION         default: "SINGAPORE"
        LEDGR_PLAYGROUND_SOFTWARE       default: "qbs"
        LEDGR_PLAYGROUND_CURRENCY       default: "SGD"
        LEDGR_PLAYGROUND_TAX_REGISTERED default: "true"
        LEDGR_PLAYGROUND_FYE_MONTH      default: 12

    Or a JSON file at ``playground_profile.json`` (resolved relative to the
    current working directory) with the same keys.
    """
    import json as _json
    import logging as _logging
    from pathlib import Path as _Path

    defaults: dict = {
        "client_id": "playground",
        "client_name": "Playground Client",
        "client_uen": "",
        "region": "SINGAPORE",
        "software": "qbs",
        "base_currency": "SGD",
        "tax_registered": True,
        "fye_month": 12,
    }

    # Env-var overrides (string -> typed coercion).
    import os as _os

    env_map = {
        "client_id": ("LEDGR_PLAYGROUND_CLIENT_ID", str),
        "client_name": ("LEDGR_PLAYGROUND_CLIENT_NAME", str),
        "client_uen": ("LEDGR_PLAYGROUND_CLIENT_UEN", str),
        "region": ("LEDGR_PLAYGROUND_REGION", str),
        "software": ("LEDGR_PLAYGROUND_SOFTWARE", str),
        "base_currency": ("LEDGR_PLAYGROUND_CURRENCY", str),
        "fye_month": ("LEDGR_PLAYGROUND_FYE_MONTH", int),
    }
    for key, (var, caster) in env_map.items():
        raw = _os.environ.get(var)
        if raw is None or raw == "":
            continue
        try:
            defaults[key] = caster(raw)
        except (TypeError, ValueError):
            _logging.getLogger(__name__).warning(
                "Ignoring invalid %s=%r (expected %s)", var, raw, caster.__name__,
            )

    tax_raw = _os.environ.get("LEDGR_PLAYGROUND_TAX_REGISTERED")
    if tax_raw is not None and tax_raw != "":
        defaults["tax_registered"] = tax_raw.strip().lower() in ("true", "1", "yes", "y")

    # JSON-file override (higher precedence than env vars).
    config_path = _Path(_os.environ.get("LEDGR_PLAYGROUND_PROFILE_PATH", "playground_profile.json"))
    coa_rows: list = []
    category_mapping: dict = {}
    entity_memory: list = []
    if config_path.is_file():
        try:
            loaded = _json.loads(config_path.read_text())
            if isinstance(loaded, dict):
                for key in defaults:
                    if key in loaded:
                        defaults[key] = loaded[key]
                # Phase 8 / playground-coa-seed: also seed COA, category
                # mapping, and entity_memory from the JSON profile when
                # present, so the categorize LLM has real accounts to match
                # against (empty coa[] previously caused account_code="" in
                # the YAU LEE ADK session).
                coa_rows = list(loaded.get("coa") or [])
                category_mapping = dict(loaded.get("category_mapping") or {})
                entity_memory = list(loaded.get("entity_memory") or [])
                _logging.getLogger(__name__).info(
                    "playground seed: loaded %d profile keys from %s",
                    len(loaded), config_path,
                )
        except (OSError, ValueError) as exc:
            _logging.getLogger(__name__).warning(
                "Failed to read playground profile from %s: %s", config_path, exc,
            )

    # Build CoaAccount / EntityMemoryEntry objects the categorizer can read
    # out of state via ``coa_from_state`` / ``entity_memory_from_state`` —
    # they expect dataclass instances, not raw dicts.
    from invoice_processing.export.client_context import CoaAccount, EntityMemoryEntry
    coa_objects = [
        CoaAccount(
            code=row.get("code"),
            description=row.get("description") or row.get("key") or "",
            account_type=row.get("account_type"),
            financial_statement=row.get("financial_statement"),
            nature=row.get("nature"),
            keywords=row.get("keywords"),
        )
        for row in coa_rows
        if isinstance(row, dict)
    ]
    entity_memory_objects = [
        EntityMemoryEntry(
            name=row.get("name") or "",
            reg_no=row.get("reg_no"),
            mapping_code=row.get("mapping_code"),
            role=row.get("role"),
            tax_code=row.get("tax_code"),
        )
        for row in entity_memory
        if isinstance(row, dict) and row.get("name")
    ]

    return ClientContext(
        client_id=defaults["client_id"],
        client_name=defaults["client_name"],
        client_uen=defaults["client_uen"] or None,
        region=defaults["region"],
        accounting_software=defaults["software"],
        base_currency=defaults["base_currency"],
        tax_registered=bool(defaults["tax_registered"]),
        fye_month=defaults["fye_month"],
        coa=coa_objects,
        category_mapping=category_mapping,
        entity_memory=entity_memory_objects,
    )


def load_client_profile(callback_context: CallbackContext):
    """2.2.0 ``before_agent_callback``: load the channel's client profile into state.

    Thin adapter over the parked ``make_load_client_by_channel_callback``. Reads
    ``state["channel_id"]`` (the Slack layer sets it = session/user id), resolves
    the client from Firestore, and writes ``ClientContext.to_state()`` keys into
    state. Always returns ``None`` (ADK convention: proceed with the agent run);
    any failure is swallowed by the parked callback's own guard.

    Dev playground seed (LEDGR_ENV != "prod"):
    When no profile resolves (no channel_id, or Firestore miss) AND
    ``config.is_playground_seed_enabled()`` is True, a synthetic
    ``ClientContext`` is injected so the document lane can run in ``adk web``
    / agents-cli without a Slack channel.  This seed is NEVER active in prod.
    """
    state = getattr(callback_context, "state", None)
    profile_key = "client_id"  # sentinel: to_state() always writes this key

    # Snapshot presence before the real loader runs.
    had_profile = state is not None and (
        state.get(profile_key) is not None
        or state.get("client_name") is not None
    )

    _load_client_by_channel(callback_context)

    # If a real profile was already present or was just loaded, we're done.
    if state is None:
        return None
    loaded_profile = state.get(profile_key) is not None or state.get("client_name") is not None
    if had_profile or loaded_profile:
        return None

    # No profile resolved.  Seed a synthetic one in non-prod only.
    if config.is_playground_seed_enabled():
        import logging as _logging
        default_ctx = _playground_default_context()
        _logging.getLogger(__name__).info(
            "playground seed: no client profile found; injecting ClientContext "
            "(client_id=%s, client_name=%s, software=%s)",
            default_ctx.client_id, default_ctx.client_name, default_ctx.accounting_software,
        )
        for k, v in default_ctx.to_state().items():
            state[k] = v

        # Seed ledger data from local store
        from accounting_agents.local_ledger_store import LocalLedgerStore
        local_store = LocalLedgerStore()
        client_id = state["client_id"]
        latest_fy = local_store.latest_fy(client_id)
        if latest_fy:
            rows = local_store.read_rows(client_id, latest_fy)
            state["ledger_data"] = rows
            state["ledger_row_count"] = len(rows)
            state["fy_loaded"] = latest_fy
            state["fy_pointers"] = local_store.fy_pointers(client_id)
        else:
            state["ledger_data"] = []
            state["ledger_row_count"] = 0
            state["fy_loaded"] = "none"
            state["fy_pointers"] = []

        state["processing_log"] = []
        state["pending_reviews"] = []

    return None


# --------------------------------------------------------------------------- #
# Coordinator (front-desk router LlmAgent) — schema-only, no tools
# --------------------------------------------------------------------------- #

coordinator = LlmAgent(
    name="coordinator",
    model=config.MODEL_LITE,
    mode="single_turn",
    instruction=(
        "You are the front desk of an accounting firm's document assistant. "
        "Read the user's turn and classify its intent. If a file was uploaded "
        "(an invoice, a receipt, or a bank statement), the intent is 'document'. "
        "If the user is asking a question about their ledger or bookkeeping, the "
        "intent is 'question'. Otherwise the intent is 'unknown'. Respond ONLY "
        "with the structured RouteDecision."
    ),
    output_schema=RouteDecision,
    before_agent_callback=load_client_profile,
)


# --------------------------------------------------------------------------- #
# dynamic_router — turn the coordinator's RouteDecision into a graph route
# --------------------------------------------------------------------------- #


@node
def dynamic_router(ctx, node_input) -> Event:
    """Route the coordinator's structured decision to one of three lanes.

    The coordinator LlmAgent (output_schema=RouteDecision) emits its decision as
    this node's ``node_input``. It may arrive as a ``RouteDecision`` model or a
    plain dict depending on the runner path; handle both defensively.
    """
    intent = _extract_intent(node_input)
    if intent == ROUTE_DOCUMENT:
        return Event(route=ROUTE_DOCUMENT, output={"intent": ROUTE_DOCUMENT})
    if intent == ROUTE_QUESTION:
        return Event(route=ROUTE_QUESTION, output={"intent": ROUTE_QUESTION})
    return Event(route=ROUTE_UNKNOWN, output={"intent": ROUTE_UNKNOWN})


def _extract_intent(node_input) -> str:
    """Pull the ``intent`` string out of a RouteDecision / dict / raw value."""
    if isinstance(node_input, RouteDecision):
        return node_input.intent
    if isinstance(node_input, dict):
        return str(node_input.get("intent", ROUTE_UNKNOWN))
    intent = getattr(node_input, "intent", None)
    return intent if isinstance(intent, str) else ROUTE_UNKNOWN


# --------------------------------------------------------------------------- #
# help_node — short help message for the 'unknown' lane
# --------------------------------------------------------------------------- #


@node
async def help_node(ctx) -> Event:
    """Defensive fallback when text misroutes through the coordinator graph."""
    message = (
        "I'm forwarding you to the accounting assistant — ask me about your "
        "ledger, extraction pipeline, or drop PDFs in this channel to process them."
    )
    ctx.state["help_message"] = message
    return Event(output={"message": message})


# --------------------------------------------------------------------------- #
# DocumentWorkflow — declarative sequential spine (Track A, Phase 6)
#
# ADK web's "Graph" tab displays a static ``Workflow(edges=...)`` as a proper
# pipeline (left→right), but a dynamic driver (``ctx.run_node``) shows up as a
# star from ``START``. We expose BOTH:
#
# * ``document_workflow`` — declarative sequential ``Workflow`` with conditional
#   fan-out from ``classify_node`` into per-lane sub-workflows. This is what
#   ADK web renders, so the user sees the actual pipeline order
#   (``extract → review → categorize → jurisdiction → tax → approval``)
#   instead of a star. Sub-workflows preserve the lane registry
#   (:mod:`accounting_agents.lane_config`).
#
# * ``document_workflow_driver`` — the legacy dynamic driver retained for
#   behaviour tests that exercise the imperative ``ctx.run_node`` path
#   (``tests/test_graph_wiring.py::test_driver_runs_all_nodes_present`` etc.).
#   The driver is NOT referenced by the main Apps — only the declarative
#   ``document_workflow`` is.
#
# Why both: the dynamic driver is still the most reliable way to write
# behaviour tests (every ``run_node`` is observable in a recording context),
# while the declarative workflow is what ADK web users see. They share the
# same node functions, so behaviour is identical.
# --------------------------------------------------------------------------- #


@node(rerun_on_resume=True)
async def document_workflow_driver(ctx, node_input=None):
    """Legacy imperative driver — behaviour parity only.

    ``rerun_on_resume=True`` is MANDATORY: ``ctx.run_node`` requires it, and it is
    what lets a HITL resume replay the driver from the top while the scheduler
    fast-forwards already-completed sub-nodes (so ``extract`` / ``consolidate``
    run exactly once across a pause→resume).

    Lane selection comes from :mod:`accounting_agents.lane_config` — the
    single declarative map used by ``classify_node`` (for the Event route
    label) and here (for the node iteration order). Adding a new doc type
    is one entry in :data:`lane_config.DOC_TYPE_TO_LANE`.
    """
    from .lane_config import DOC_TYPE_TO_LANE, get_node_factory

    await ctx.run_node(nodes.classify_node)

    # Lane lookup — one map, one source of truth (was an inline if/else).
    doc_type = (ctx.state.get(nodes.DOC_TYPE_KEY) or "").strip().lower()
    lane = DOC_TYPE_TO_LANE.get(doc_type) or DOC_TYPE_TO_LANE.get("other")
    for node_name in lane.node_names:
        await ctx.run_node(get_node_factory(node_name))

    # Terminal HITL gate. ``approval_gate`` is a default node (rerun_on_resume=
    # False): on the human's resume, ADK's replay interceptor completes it with
    # the ``ApproveDecision`` as its OUTPUT (Case 4 — a non-rerun node's resolved
    # response becomes its output), which ``ctx.run_node`` returns here. On the
    # auto-approve / first pass it returns None. Either way we thread the result
    # into ``apply_decision_node`` exactly as the former static edge did.
    decision = await ctx.run_node(nodes.approval_gate)
    if decision is None and getattr(ctx, "resume_inputs", None):
        # Defensive belt-and-braces: if a future ADK build ever surfaces the
        # gate decision via the driver's resume_inputs instead of the node
        # output, recover it by the gate's interrupt id rather than silently
        # dropping the human's choice.
        decision = ctx.resume_inputs.get(nodes._approval_interrupt_id(ctx.state))

    await ctx.run_node(nodes.apply_decision_node, node_input=decision)
    await ctx.run_node(nodes.route_node)
    await ctx.run_node(nodes.consolidate_node)
    return await ctx.run_node(nodes.deliver_node)


# --------------------------------------------------------------------------- #
# Lane sub-workflows — declarative sequential pipelines per doc-type lane.
#
# These are the "real" pipelines ADK web displays as left→right chains. They
# share node functions with the dynamic driver; node bodies are unchanged.
# Each lane's Workflow is ``rerun_on_resume=True`` via being a sub-Workflow
# of ``document_workflow`` (the parent carries resumability).
# --------------------------------------------------------------------------- #


def _build_lane_subworkflows():
    """Build a per-lane sub-Workflow from the lane registry.

    Returns a dict ``{route_label: Workflow}``. Each workflow's edges form a
    strict sequential chain ending at ``deliver_node`` so ADK web renders a
    single left→right pipeline. Lane node lists come from
    :data:`lane_config.DOC_TYPE_TO_LANE` — one declarative source of truth.
    """
    from . import lane_config

    pipelines: dict[str, Workflow] = {}
    seen: set[int] = set()
    for lane in lane_config.DOC_TYPE_TO_LANE.values():
        if id(lane) in seen:
            continue
        seen.add(id(lane))
        # Resolve symbolic names to @node callables for declarative edges.
        nodes_chain = [lane_config.get_node_factory(n) for n in lane.node_names]
        # Terminal spine — common to all lanes.
        terminal_chain = [
            nodes.approval_gate,
            nodes.apply_decision_node,
            nodes.route_node,
            nodes.consolidate_node,
            nodes.deliver_node,
        ]
        # Build a single sequential chain: lane nodes -> terminal spine.
        full_chain = nodes_chain + terminal_chain
        edges = list(zip([START, *full_chain[:-1]], full_chain))
        pipelines[lane.route_label] = Workflow(
            name=f"pipeline_{lane.name}",
            description=(
                f"{lane.description} → approval → apply → route → "
                "consolidate → deliver."
            ),
            edges=edges,
        )
    return pipelines


_LANE_PIPELINES: dict[str, Workflow] = _build_lane_subworkflows()


# Single declarative document workflow. ``classify_node`` returns
# ``Event(route=ROUTE_COMMERCIAL_DOC | ROUTE_BANK)`` and the conditional edge
# dispatches to the matching lane pipeline. ADK web renders this as
# ``START → classify → [bank | commercial] pipeline``.
document_workflow = Workflow(
    name="document_workflow",
    description=(
        "Classify a document, then run its lane pipeline (extract → enrich → "
        "tax → approval → deliver)."
    ),
    edges=[
        (START, nodes.classify_node),
        (
            nodes.classify_node,
            {route_label: pipeline for route_label, pipeline in _LANE_PIPELINES.items()},
        ),
    ],
)


# --------------------------------------------------------------------------- #
# Top-level coordinator graph (Track B: single-root, inlined document spine)
#
# The top-level graph is FLAT — no nested ``document_workflow`` gray box. ADK
# web renders one connected graph::
#
#     START → coordinator → dynamic_router
#       → { document: classify_node → {commercial_doc: pipeline_commercial,
#                                       bank_statement: pipeline_bank},
#           question / unknown: help_node }
#
# This satisfies the "one top-level graph" Track B goal: the user sees the
# whole pipeline in the Graph tab, drilling into ``pipeline_commercial`` only
# if they want to inspect the per-lane chain.
#
# ``document_workflow`` (and therefore ``document_app``) retains the nested
# shape — Slack prod uploads bypass the coordinator LLM noise and route
# straight into the document workflow. Per ADR-0008 / plan, two Apps is OK
# (one for full QA graph, one for prod uploads).
# --------------------------------------------------------------------------- #

coordinator_graph = Workflow(
    name="coordinator_graph",
    description=(
        "Front-desk router dispatching to document lane (classify → extract → "
        "tax → approval → deliver) or help lane."
    ),
    edges=[
        # START → coordinator → dynamic_router (unconditional chain).
        (START, coordinator, dynamic_router),
        # ROUTE_DOCUMENT: dispatch into the inlined document spine via an
        # Edge with the ROUTE_DOCUMENT route label. The document_workflow
        # edges (classify → lane pipelines) are inlined here so the
        # top-level graph shows ONE pipeline.
        Edge(
            from_node=dynamic_router,
            to_node=nodes.classify_node,
            route=ROUTE_DOCUMENT,
        ),
        # classify_node fans out to the lane pipeline via the Event.route
        # label written by classify_node. Both lanes (commercial_doc,
        # bank_statement) come from the lane registry. Dict-style
        # conditional dispatch is only supported as a tuple element — wrap
        # in a 2-tuple ``(classify_node, {label: pipeline})``.
        (nodes.classify_node, {route_label: pipeline for route_label, pipeline in _LANE_PIPELINES.items()}),
        # Text/question traffic is handled by the standalone ``assistant_app``
        # (ADR-0008); the chat lane no longer runs through this graph. Keep
        # the ``ROUTE_QUESTION`` label wired to ``help_node`` (shared with
        # ``ROUTE_UNKNOWN``) as a defensive fallback in case a file_shared
        # path ever misroutes here. ADK rejects two (from, to) edges with the
        # same endpoints, so the two route labels live on a single ``Edge``
        # with ``route=[...]`` instead of separate dict entries.
        Edge(
            from_node=dynamic_router,
            to_node=help_node,
            route=[ROUTE_QUESTION, ROUTE_UNKNOWN],
        ),
    ],
)


# --------------------------------------------------------------------------- #
# Apps — document coordinator + standalone chat assistant
# --------------------------------------------------------------------------- #

# ADK AgentEvaluator / ``adk eval`` convention — document-lane golden cases load
# this module and read ``root_agent`` directly (not ``app.root_agent``).
root_agent = coordinator_graph

app = App(
    name="accounting_agents",
    root_agent=coordinator_graph,
    resumability_config=ResumabilityConfig(is_resumable=True),
)

#: Direct document workflow App — skips coordinator LLM on file uploads (P2-2).
document_app = App(
    name="accounting_agents_document",
    root_agent=document_workflow,
    resumability_config=ResumabilityConfig(is_resumable=True),
)

#: Standalone chat-lane App: a root ``LlmAgent`` with no ``mode`` so it sees
#: full per-thread session history (multi-turn). Has its own Runner built by
#: ``slack_runner.build_chat_runner``. No ``resumability_config`` — chat has no
#: HITL gates. P7: ``LedgrReflectRetryPlugin`` retries tools that return
#: ``status=error|not_found``. See ADR-0008 / ADR-0013.
assistant_app = App(
    name="accounting_agents_assistant",
    root_agent=assistant_agent,
    plugins=[LedgrReflectRetryPlugin(max_retries=2)],
)

__all__ = [
    "app",
    "document_app",
    "assistant_app",
    "assistant_agent",
    "coordinator",
    "coordinator_graph",
    "document_workflow",
    "RouteDecision",
]
