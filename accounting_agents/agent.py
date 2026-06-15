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

DocumentWorkflow (resumable, dynamic — Step 6)::

    START -> document_workflow_driver   (single @node(rerun_on_resume=True))

The driver runs the SAME node functions in program order via ``ctx.run_node``::

    classify_node
      -> { invoice:        extract_invoice_node -> review_extraction_node (may pause)
                           -> categorize_node -> tax_node,
           bank_statement: extract_bank_node }
      -> approval_gate (may pause) -> apply_decision_node -> route_node
      -> consolidate_node -> deliver_node

This is a behavior-preserving refactor of the former static DAG (Option (c)): the
node BODIES are untouched; only the scheduling moves from a declarative edge list
into an imperative driver. ``ctx.run_node`` dedups already-checkpointed sub-nodes
on resume, so side-effecting nodes (extract / consolidate) run exactly once even
though the ``rerun_on_resume=True`` driver replays from the top after every pause.

HITL is preserved unchanged because interrupt correlation is by ``interrupt_id``
string (independent of scheduling):
- Mid-flow ``:review`` pause: ``review_extraction_node`` is itself
  ``rerun_on_resume=True`` and reads its decision from ``ctx.resume_inputs`` — the
  driver simply re-runs it on resume and it applies its own decision.
- Terminal pause: ``approval_gate`` is a default node, so on resume ADK's replay
  interceptor completes it with the human's ``ApproveDecision`` as its OUTPUT
  (``_replay_interceptor.check_interception`` Case 4 — ``rerun_on_resume=False``
  → resolved response becomes the node output). ``ctx.run_node(approval_gate)``
  therefore RETURNS that decision, which the driver threads into
  ``apply_decision_node`` as its ``node_input`` — exactly the shape the former
  static edge ``(approval_gate, apply_decision_node)`` delivered.

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
    FirestoreClientStore,
    make_load_client_by_channel_callback,
)

from . import config  # ensures AI Studio env is set before any ADK model init
from . import nodes
from .assistant import assistant_agent  # noqa: F401 — re-exported via __all__

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
# DocumentWorkflow — deterministic spine (resumable / dynamic driver, Step 6)
#
# Option (c): a thin dynamic driver that wraps the existing node functions
# UNCHANGED. The driver runs each node via ``ctx.run_node`` in program order;
# ADK's dynamic scheduler dedups already-checkpointed sub-nodes on resume, so the
# side-effecting nodes run exactly once even though the driver replays from the
# top after every HITL pause. See the module docstring for the HITL-preservation
# argument (interrupt correlation is by ``interrupt_id`` string, unchanged).
# --------------------------------------------------------------------------- #


@node(rerun_on_resume=True)
async def document_workflow_driver(ctx, node_input=None):
    """Imperative driver replacing the former static DocumentWorkflow DAG.

    ``rerun_on_resume=True`` is MANDATORY: ``ctx.run_node`` requires it, and it is
    what lets a HITL resume replay the driver from the top while the scheduler
    fast-forwards already-completed sub-nodes (so ``extract`` / ``consolidate``
    run exactly once across a pause→resume).

    Branch on ``state[DOC_TYPE_KEY]`` (the value ``classify_node`` persists:
    ``"bank_statement"`` for the bank lane, the lowercased doc type otherwise) —
    NOT on the classify route Event, since the driver schedules nodes directly.
    """
    await ctx.run_node(nodes.classify_node)

    if ctx.state.get(nodes.DOC_TYPE_KEY) == nodes.ROUTE_BANK:
        # Bank-statement lane (MODEL_STD): single extraction node.
        await ctx.run_node(nodes.extract_bank_node)
    else:
        # Invoice / receipt lane (MODEL_LITE): extract -> review -> categorize ->
        # tax. ``review_extraction_node`` (itself rerun_on_resume=True) may pause
        # mid-flow with the ``:review`` interrupt; on resume it applies its own
        # ``ReviewClarifyDecision`` from ``ctx.resume_inputs`` and falls through.
        await ctx.run_node(nodes.extract_invoice_node)
        await ctx.run_node(nodes.review_extraction_node)
        await ctx.run_node(nodes.categorize_node)
        await ctx.run_node(nodes.tax_node)

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


document_workflow = Workflow(
    name="document_workflow",
    description="Classify a document, extract+enrich it, gate, route, and deliver.",
    edges=[(START, document_workflow_driver)],
)


# --------------------------------------------------------------------------- #
# Top-level coordinator graph
# --------------------------------------------------------------------------- #

coordinator_graph = Workflow(
    name="coordinator_graph",
    description="Front-desk router dispatching to document / help lanes.",
    edges=[
        (START, coordinator, dynamic_router),
        (
            dynamic_router,
            {ROUTE_DOCUMENT: document_workflow},
        ),
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

app = App(
    name="accounting_agents",
    root_agent=coordinator_graph,
    resumability_config=ResumabilityConfig(is_resumable=True),
)

#: Standalone chat-lane App: a root ``LlmAgent`` with no ``mode`` so it sees
#: full per-thread session history (multi-turn). Has its own Runner built by
#: ``slack_runner.build_chat_runner``. No ``resumability_config`` — chat has no
#: HITL gates. See ADR-0008.
assistant_app = App(
    name="accounting_agents_assistant",
    root_agent=assistant_agent,
)

__all__ = [
    "app",
    "assistant_app",
    "assistant_agent",
    "coordinator",
    "coordinator_graph",
    "document_workflow",
    "RouteDecision",
]
