"""ADK 2.0 graph for the Ledgr accounting agent system.

Top-level shape (verified against google-adk 2.2.0)::

    START
      -> coordinator        (LlmAgent, output_schema=RouteDecision,
                             before_agent_callback loads the channel's profile)
      -> dynamic_router     (@node -> Event(route=...))
      -> { "document": DocumentWorkflow,
           "question": qa_agent,
           "unknown":  help_node }

DocumentWorkflow (resumable, dynamic)::

    START -> classify_node
      -> { "invoice":        extract_invoice_node -> categorize_node -> tax_node,
           "bank_statement": extract_bank_node }
      -> approval_gate -> route_node -> consolidate_node -> deliver_node

Convergence note: ``classify_node`` emits exactly one route per document, so only
ONE branch fires. A plain graph node runs when ANY predecessor triggers it (it
does not require all predecessors), so both the invoice tail (``tax_node``) and
the bank tail (``extract_bank_node``) can simply edge into the shared
``approval_gate`` successor — no JoinNode needed.

API facts grounded in the installed 2.2.0 source (see the task report for detail):
- ``Workflow(name=..., edges=[...])``; chains are tuples ``(START, a, b)``; a
  conditional fan-out is a dict element ``{"route": node_or_(tuple)}``; a node
  reached by multiple edges is the convergence point.
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
from google.adk.workflow import START, Workflow, node
from pydantic import BaseModel, Field

from invoice_processing.export.client_context import (
    FirestoreClientStore,
    make_load_client_by_channel_callback,
)

from . import config  # ensures AI Studio env is set before any ADK model init
from . import nodes
from .qa_agent import qa_agent  # noqa: F401 — re-exported via __all__

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


def load_client_profile(callback_context: CallbackContext):
    """2.2.0 ``before_agent_callback``: load the channel's client profile into state.

    Thin adapter over the parked ``make_load_client_by_channel_callback``. Reads
    ``state["channel_id"]`` (the Slack layer sets it = session/user id), resolves
    the client from Firestore, and writes ``ClientContext.to_state()`` keys into
    state. Always returns ``None`` (ADK convention: proceed with the agent run);
    any failure is swallowed by the parked callback's own guard.
    """
    return _load_client_by_channel(callback_context)


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
    """Return a short help message when the coordinator can't classify the turn."""
    message = (
        "I help process accounting documents. Upload an invoice, receipt, or bank "
        "statement and I'll extract, categorize, and add it to your ledger — or "
        "ask me a question about your ledger."
    )
    ctx.state["help_message"] = message
    return Event(output={"message": message})


# --------------------------------------------------------------------------- #
# DocumentWorkflow — deterministic spine (resumable / dynamic)
# --------------------------------------------------------------------------- #

document_workflow = Workflow(
    name="document_workflow",
    description="Classify a document, extract+enrich it, gate, route, and deliver.",
    edges=[
        # Entry: classify the uploaded PDF, then fan out by document type.
        (
            START,
            nodes.classify_node,
            {
                # Invoice / receipt lane (MODEL_LITE): extract -> categorize -> tax.
                nodes.ROUTE_INVOICE: nodes.extract_invoice_node,
                # Bank-statement lane (MODEL_STD): single extraction node.
                nodes.ROUTE_BANK: nodes.extract_bank_node,
            },
        ),
        # Invoice lane chain.
        (nodes.extract_invoice_node, nodes.categorize_node, nodes.tax_node),
        # Convergence: both lane tails edge into the shared approval gate. Only
        # one lane fires per document (classify emits a single route), so the
        # gate runs exactly once.
        (nodes.tax_node, nodes.approval_gate),
        (nodes.extract_bank_node, nodes.approval_gate),
        # Post-approval spine: route -> consolidate -> deliver (terminal).
        (
            nodes.approval_gate,
            nodes.route_node,
            nodes.consolidate_node,
            nodes.deliver_node,
        ),
    ],
)


# --------------------------------------------------------------------------- #
# Top-level coordinator graph
# --------------------------------------------------------------------------- #

coordinator_graph = Workflow(
    name="coordinator_graph",
    description="Front-desk router dispatching to document / question / help lanes.",
    edges=[
        (START, coordinator, dynamic_router),
        (
            dynamic_router,
            {
                ROUTE_DOCUMENT: document_workflow,
                ROUTE_QUESTION: qa_agent,
                ROUTE_UNKNOWN: help_node,
            },
        ),
    ],
)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

app = App(
    name="accounting_agents",
    root_agent=coordinator_graph,
    resumability_config=ResumabilityConfig(is_resumable=True),
)

__all__ = ["app", "coordinator", "coordinator_graph", "document_workflow", "RouteDecision"]
