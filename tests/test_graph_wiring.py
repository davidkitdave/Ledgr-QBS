"""Structural wiring tests for the ADK 2.0 accounting graph (accounting_agents.agent).

These assert the App / Workflow are constructed and the nodes/edges are wired in
the expected order WITHOUT any network call (no Gemini, no Firestore, no real
session or artifact service). They also drive the post-classification placeholder
spine (approval_gate -> route_node -> consolidate_node -> deliver_node) directly
with a fake Context to prove a document pass reaches ``deliver_node``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from google.adk.apps import App
from google.adk.workflow import Workflow

from accounting_agents import nodes
from accounting_agents.agent import (
    ROUTE_DOCUMENT,
    ROUTE_QUESTION,
    ROUTE_UNKNOWN,
    RouteDecision,
    app,
    coordinator,
    coordinator_graph,
    document_workflow,
    document_workflow_driver,
    dynamic_router,
)


# =========================================================================== #
# Helpers
# =========================================================================== #


def _edges(wf: Workflow) -> list[tuple[str, str, object]]:
    """(from_name, to_name, route) triples for a workflow's compiled graph."""
    return [(e.from_node.name, e.to_node.name, e.route) for e in wf.graph.edges]


def _node_names(wf: Workflow) -> set[str]:
    return {n.name for n in wf.graph.nodes}


class _FakeContext:
    """Duck-typed stand-in for google.adk.agents.context.Context (state only)."""

    def __init__(self, state: dict):
        self.state = dict(state)


class _RecordingContext:
    """Fake Context that records ``ctx.run_node`` calls in program order.

    Drives the dynamic ``document_workflow_driver`` WITHOUT a real scheduler:
    each ``run_node(node, ...)`` is recorded as a ``(node_name, node_input)``
    pair and resolved with a per-node scripted return value (default ``None``).
    A ``state_writes`` map lets a test simulate what a node would write to state
    (e.g. ``classify_node`` setting ``DOC_TYPE_KEY``) so the driver's branch +
    terminal-decision threading can be asserted deterministically.
    """

    def __init__(self, state: dict, *, returns: dict | None = None,
                 state_writes: dict | None = None, resume_inputs: dict | None = None):
        self.state = dict(state)
        self.calls: list[tuple[str, object]] = []
        self._returns = returns or {}
        self._state_writes = state_writes or {}
        self.resume_inputs = dict(resume_inputs or {})

    async def run_node(self, node, node_input=None, **_kwargs):
        name = getattr(node, "name", getattr(node, "__name__", str(node)))
        self.calls.append((name, node_input))
        if name in self._state_writes:
            self.state.update(self._state_writes[name])
        return self._returns.get(name)

    @property
    def call_names(self) -> list[str]:
        return [name for name, _inp in self.calls]


def _drive_driver(ctx: _RecordingContext):
    """Run the real ``document_workflow_driver`` against a recording ctx."""
    fn = document_workflow_driver._func
    return asyncio.run(fn(ctx))


# =========================================================================== #
# App construction
# =========================================================================== #


def test_app_constructed_and_resumable():
    assert isinstance(app, App)
    assert app.name == "accounting_agents"
    assert app.root_agent is coordinator_graph
    assert isinstance(app.root_agent, Workflow)
    assert app.resumability_config is not None
    assert app.resumability_config.is_resumable is True


def test_coordinator_has_profile_callback_and_schema():
    # Schema-only router: structured output, no tools.
    assert coordinator.output_schema is RouteDecision
    assert not getattr(coordinator, "tools", None)
    # mode must be single_turn to be usable as a graph node.
    assert coordinator.mode == "single_turn"
    # before_agent_callback wires the channel profile loader.
    assert coordinator.before_agent_callback is not None


# =========================================================================== #
# Top-level coordinator graph wiring
# =========================================================================== #


def test_coordinator_graph_nodes_present():
    names = _node_names(coordinator_graph)
    # The graph carries the document + help lanes; chat runs on a separate
    # standalone ``assistant_app`` (ADR-0008), so ``assistant`` is NOT a node.
    assert {
        "__START__",
        "coordinator",
        "dynamic_router",
        "document_workflow",
        "help_node",
    } <= names
    assert "assistant" not in names
    assert "qa_agent" not in names


def test_coordinator_graph_start_chain_and_routes():
    edges = _edges(coordinator_graph)
    # START -> coordinator -> dynamic_router (unconditional chain).
    assert ("__START__", "coordinator", None) in edges
    assert ("coordinator", "dynamic_router", None) in edges
    # dynamic_router fans out by route label. The question lane is repointed
    # to help_node as a defensive fallback (real text goes through assistant_app);
    # ADK rejects duplicate (from, to) edges, so the question + unknown labels
    # share a single edge carrying ``route=[ROUTE_QUESTION, ROUTE_UNKNOWN]``.
    assert ("dynamic_router", "document_workflow", ROUTE_DOCUMENT) in edges
    help_routes = {
        tuple(r) if isinstance(r, list) else (r,)
        for frm, to, r in edges
        if frm == "dynamic_router" and to == "help_node"
    }
    assert any(
        ROUTE_QUESTION in r and ROUTE_UNKNOWN in r for r in help_routes
    ), f"expected a (dynamic_router → help_node) edge covering both routes, got {help_routes}"


def test_qa_agent_not_imported_from_agent():
    """``agent.py`` no longer imports the retired qa_agent symbol (ADR-0008)."""
    import accounting_agents.agent as _agent

    assert not hasattr(_agent, "qa_agent")
    # The standalone chat app is exported instead.
    assert hasattr(_agent, "assistant_app")
    assert hasattr(_agent, "assistant_agent")


# =========================================================================== #
# DocumentWorkflow wiring — Step 6: a single dynamic driver replaces the static
# DAG. The wiring is now an IMPERATIVE node-run SEQUENCE inside
# ``document_workflow_driver`` (not declarative edges), so we assert the order of
# ``ctx.run_node`` calls for each branch instead of (from, to, route) triples.
# =========================================================================== #


def test_document_workflow_is_single_driver_edge():
    # The Workflow now holds exactly one edge: START -> the driver node.
    assert _node_names(document_workflow) == {"__START__", "document_workflow_driver"}
    assert _edges(document_workflow) == [("__START__", "document_workflow_driver", None)]
    # The driver MUST rerun on resume (ctx.run_node requires it; it is what lets
    # a HITL resume replay the driver while sub-nodes are fast-forwarded).
    assert document_workflow_driver.rerun_on_resume is True


def test_driver_runs_all_nodes_present():
    """Driving the invoice branch touches every spine node exactly once."""
    ctx = _RecordingContext(
        state={},
        state_writes={"classify_node": {nodes.DOC_TYPE_KEY: nodes.ROUTE_INVOICE}},
    )
    _drive_driver(ctx)
    assert set(ctx.call_names) == {
        "classify_node",
        "extract_invoice_node",
        "review_extraction_node",
        "categorize_node",
        "tax_node",
        "approval_gate",
        "apply_decision_node",
        "route_node",
        "consolidate_node",
        "deliver_node",
    }


def test_driver_classify_fanout_invoice_branch():
    """classify writing a non-bank doc_type drives the invoice lane (NOT bank)."""
    ctx = _RecordingContext(
        state={},
        state_writes={"classify_node": {nodes.DOC_TYPE_KEY: nodes.ROUTE_INVOICE}},
    )
    _drive_driver(ctx)
    assert ctx.call_names[0] == "classify_node"
    assert "extract_invoice_node" in ctx.call_names
    assert "extract_bank_node" not in ctx.call_names


def test_driver_classify_fanout_bank_branch():
    """classify writing ``bank_statement`` drives the single bank extraction node."""
    ctx = _RecordingContext(
        state={},
        state_writes={"classify_node": {nodes.DOC_TYPE_KEY: nodes.ROUTE_BANK}},
    )
    _drive_driver(ctx)
    assert ctx.call_names[0] == "classify_node"
    assert "extract_bank_node" in ctx.call_names
    # Invoice-lane nodes never run on the bank branch.
    for n in ("extract_invoice_node", "review_extraction_node",
              "categorize_node", "tax_node"):
        assert n not in ctx.call_names


def test_driver_invoice_lane_chain_order():
    """The invoice lane runs extract -> review -> categorize -> tax in order."""
    ctx = _RecordingContext(
        state={},
        state_writes={"classify_node": {nodes.DOC_TYPE_KEY: nodes.ROUTE_INVOICE}},
    )
    _drive_driver(ctx)
    names = ctx.call_names
    chain = ["extract_invoice_node", "review_extraction_node", "categorize_node", "tax_node"]
    idxs = [names.index(n) for n in chain]
    assert idxs == sorted(idxs)
    # The reviewer sits BETWEEN extraction and categorization.
    assert names.index("review_extraction_node") > names.index("extract_invoice_node")
    assert names.index("review_extraction_node") < names.index("categorize_node")


def test_driver_branches_converge_on_approval_gate():
    """Both lanes reach approval_gate before the post-approval spine."""
    for write, lane_tail in (
        ({nodes.DOC_TYPE_KEY: nodes.ROUTE_INVOICE}, "tax_node"),
        ({nodes.DOC_TYPE_KEY: nodes.ROUTE_BANK}, "extract_bank_node"),
    ):
        ctx = _RecordingContext(state={}, state_writes={"classify_node": write})
        _drive_driver(ctx)
        names = ctx.call_names
        assert "approval_gate" in names
        # The gate runs after the lane tail and before apply_decision_node.
        assert names.index(lane_tail) < names.index("approval_gate")
        assert names.index("approval_gate") < names.index("apply_decision_node")


def test_driver_post_approval_spine_order():
    """approval_gate -> apply_decision_node -> route -> consolidate -> deliver."""
    ctx = _RecordingContext(
        state={},
        state_writes={"classify_node": {nodes.DOC_TYPE_KEY: nodes.ROUTE_INVOICE}},
    )
    _drive_driver(ctx)
    names = ctx.call_names
    spine = ["approval_gate", "apply_decision_node", "route_node",
             "consolidate_node", "deliver_node"]
    idxs = [names.index(n) for n in spine]
    assert idxs == sorted(idxs)
    # deliver_node is terminal (last node the driver runs).
    assert names[-1] == "deliver_node"


def test_driver_threads_terminal_decision_to_apply_decision_node():
    """The terminal gate's return value is threaded into apply_decision_node.

    On resume, ``ctx.run_node(approval_gate)`` returns the human's ApproveDecision
    (ADK's replay interceptor completes the non-rerun gate with the resolved
    response as its output). The driver must pass that SAME object as
    apply_decision_node's ``node_input`` — mirroring the former static edge
    ``(approval_gate, apply_decision_node)``.
    """
    decision = {"decision": "approve"}
    ctx = _RecordingContext(
        state={},
        state_writes={"classify_node": {nodes.DOC_TYPE_KEY: nodes.ROUTE_INVOICE}},
        returns={"approval_gate": decision},
    )
    _drive_driver(ctx)
    apply_inputs = [inp for name, inp in ctx.calls if name == "apply_decision_node"]
    assert apply_inputs == [decision]


def test_driver_auto_approve_threads_none_to_apply_decision_node():
    """On the auto-approve / first-pass path the gate returns None → node_input None."""
    ctx = _RecordingContext(
        state={},
        state_writes={"classify_node": {nodes.DOC_TYPE_KEY: nodes.ROUTE_INVOICE}},
        # approval_gate returns None (no decision yet).
    )
    _drive_driver(ctx)
    apply_inputs = [inp for name, inp in ctx.calls if name == "apply_decision_node"]
    assert apply_inputs == [None]


def test_driver_recovers_decision_from_resume_inputs_fallback():
    """If the gate output is None but resume_inputs holds the decision, recover it.

    Defensive belt-and-braces path: keyed by the gate's interrupt id.
    """
    decision = {"decision": "edit"}
    gate_id = "C9:F9"
    ctx = _RecordingContext(
        state={"op_id": gate_id},
        state_writes={"classify_node": {nodes.DOC_TYPE_KEY: nodes.ROUTE_INVOICE}},
        resume_inputs={gate_id: decision},
        # approval_gate returns None, forcing the resume_inputs fallback.
    )
    _drive_driver(ctx)
    apply_inputs = [inp for name, inp in ctx.calls if name == "apply_decision_node"]
    assert apply_inputs == [decision]


# =========================================================================== #
# dynamic_router behavior (no network)
# =========================================================================== #


def _run_router(node_input):
    ctx = _FakeContext(state={})
    # FunctionNode stores the wrapped callable on the ``_func`` PrivateAttr.
    fn = dynamic_router._func
    result = fn(ctx, node_input)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return result


def test_dynamic_router_maps_each_intent():
    assert _run_router(RouteDecision(intent="document")).actions.route == ROUTE_DOCUMENT
    assert _run_router(RouteDecision(intent="question")).actions.route == ROUTE_QUESTION
    assert _run_router(RouteDecision(intent="unknown")).actions.route == ROUTE_UNKNOWN
    # Dict / unexpected input falls back to 'unknown' without raising.
    assert _run_router({"intent": "question"}).actions.route == ROUTE_QUESTION
    assert _run_router(object()).actions.route == ROUTE_UNKNOWN


# =========================================================================== #
# Placeholder spine reaches deliver_node (document pass, fakes only)
# =========================================================================== #


def test_placeholder_spine_invoice_pass_reaches_deliver():
    """Drive approval_gate -> apply_decision_node -> route_node -> consolidate_node
    -> deliver_node with a fake Context carrying one normalized invoice, proving
    the spine is runnable and terminates at deliver_node (no Gemini / artifact /
    Firestore)."""
    state = {
        nodes.DOC_TYPE_KEY: "invoice",
        nodes.DIRECTION_KEY: "purchase",
        "client_id": "test-client",
        "fye_month": 3,
        # consolidate_node no longer falls back silently to "qbs" when software
        # is missing — the runner seeds it from the per-channel client profile
        # in production. This unit test drives consolidate_node directly, so we
        # seed the state explicitly to keep it self-contained.
        "software": "qbs",
        nodes.NORMALIZED_KEY: [
            {
                "doc_type": "purchase",
                "invoice_number": "INV-1",
                "invoice_date": "2026-01-15",
                "currency": "SGD",
                "supplier": {"name": "Acme"},
                "customer": {},
                "lines": [],
                "our_gst_registered": True,
                "reconciled": True,
            }
        ],
    }
    ctx = _FakeContext(state=state)

    # Fully reconciled, no flagged / low-confidence lines -> the gate
    # auto-approves and yields NO RequestInput (it's an async generator now).
    interrupts = _drain_gate(ctx)
    assert interrupts == []
    assert ctx.state["approval_status"] == "auto_approved"

    # apply_decision_node sees no resume input (auto-approve) -> passthrough.
    asyncio.run(nodes.apply_decision_node(ctx, None))

    asyncio.run(nodes.route_node(ctx))
    assert len(ctx.state[nodes.ROUTES_KEY]) == 1

    consolidate = asyncio.run(nodes.consolidate_node(ctx))
    assert consolidate.output["consolidated"] == 1
    # consolidate_node prepares a Slack-agnostic ledger payload in state.
    assert ctx.state[nodes.LEDGER_ROWS_KEY]["kind"] == "invoice"
    assert len(ctx.state[nodes.LEDGER_ROWS_KEY]["batches"]) == 1

    deliver = asyncio.run(nodes.deliver_node(ctx))
    assert deliver.output["delivered"] is True
    assert ctx.state["delivered"] is True


def test_placeholder_spine_bank_pass_reaches_deliver():
    """Same spine via the bank lane (one BankStatement) -> deliver_node."""
    state = {
        nodes.DOC_TYPE_KEY: "bank_statement",
        nodes.DIRECTION_KEY: None,
        "client_id": "test-client",
        "fye_month": 3,
        nodes.BANK_STATEMENTS_KEY: [
            {
                "account_number": "123-456",
                "currency": "SGD",
                "transactions": [{"date": "2026-02-10", "description": "x", "amount": 10.0}],
            }
        ],
    }
    ctx = _FakeContext(state=state)

    # Bank lane has no normalized invoices to inspect -> gate auto-approves.
    assert _drain_gate(ctx) == []
    assert ctx.state["approval_status"] == "auto_approved"
    # apply_decision_node sees no resume input (auto-approve) -> passthrough.
    asyncio.run(nodes.apply_decision_node(ctx, None))
    asyncio.run(nodes.route_node(ctx))
    assert len(ctx.state[nodes.ROUTES_KEY]) == 1
    assert asyncio.run(nodes.consolidate_node(ctx)).output["consolidated"] == 1
    assert ctx.state[nodes.LEDGER_ROWS_KEY]["kind"] == "bank"
    assert asyncio.run(nodes.deliver_node(ctx)).output["delivered"] is True


def _drain_gate(ctx) -> list:
    """Drive the ``approval_gate`` async generator; return any yielded items.

    The gate yields a ``RequestInput`` only when the document needs human
    review; on the auto-approve path it yields nothing. Returns the list of
    yielded items so tests can assert pause vs pass-through.
    """

    async def _collect() -> list:
        return [item async for item in nodes.approval_gate(ctx)]

    return asyncio.run(_collect())
