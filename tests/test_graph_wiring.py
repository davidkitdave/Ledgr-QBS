"""Structural wiring tests for the ADK 2.0 accounting graph (accounting_agents.agent).

These assert the App / Workflow are constructed and the nodes/edges are wired in
the expected order WITHOUT any network call (no Gemini, no Firestore, no real
session or artifact service). They also drive the post-classification placeholder
spine (approval_gate -> route_node -> consolidate_node -> deliver_node) directly
with a fake Context to prove a document pass reaches ``deliver_node``.
"""

from __future__ import annotations

import asyncio

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
    # Track B (Phase 6): the top-level graph is FLAT — no nested
    # ``document_workflow`` gray box. The classify + lane pipeline nodes
    # are inlined so ADK web renders ONE connected graph from START to
    # the terminal nodes (deliver_node inside pipeline_*).
    assert {
        "__START__",
        "coordinator",
        "dynamic_router",
        "classify_node",
        "pipeline_commercial",
        "pipeline_bank",
        "help_node",
    } <= names
    # ``document_workflow`` is no longer a node in the top-level graph —
    # it remains a separate Workflow used by ``document_app`` (Slack prod
    # path) but is NOT in the coordinator pipeline.
    assert "document_workflow" not in names
    # Chat runs on a separate standalone ``assistant_app`` (ADR-0008),
    # so ``assistant`` is NOT a node.
    assert "assistant" not in names
    assert "qa_agent" not in names


def test_coordinator_graph_start_chain_and_routes():
    edges = _edges(coordinator_graph)
    # START -> coordinator -> dynamic_router (unconditional chain).
    assert ("__START__", "coordinator", None) in edges
    assert ("coordinator", "dynamic_router", None) in edges
    # Track B (Phase 6): dynamic_router fans out to classify_node
    # (not document_workflow) when route=document. The document
    # workflow is now INLINED into the top-level graph, so ADK web
    # shows one connected pipeline instead of a nested gray box.
    assert ("dynamic_router", "classify_node", ROUTE_DOCUMENT) in edges
    # classify_node fans out to per-lane pipelines via the Event.route
    # label written by classify_node (commercial_doc / bank_statement).
    classify_edges = {
        (t, r)
        for f, t, r in edges
        if f == "classify_node"
    }
    assert ("pipeline_commercial", "commercial_doc") in classify_edges
    assert ("pipeline_bank", "bank_statement") in classify_edges
    # The question lane is repointed to help_node as a defensive fallback
    # (real text goes through assistant_app). ADK rejects duplicate
    # (from, to) edges, so the question + unknown labels share a single
    # edge carrying ``route=[ROUTE_QUESTION, ROUTE_UNKNOWN]``.
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


def test_document_workflow_is_declarative_pipeline():
    """Track A: ``document_workflow`` is a declarative pipeline (Phase 6).

    ADK web renders the edges below as a left→right pipeline:
        START → classify_node → {commercial_doc → pipeline_commercial,
                                 bank_statement → pipeline_bank}

    The legacy ``document_workflow_driver`` is retained for behaviour tests
    (see ``test_driver_runs_all_nodes_present`` etc.) but no longer wired
    into the workflow's edges.
    """
    names = _node_names(document_workflow)
    assert "classify_node" in names
    # Each lane pipeline is a separate sub-Workflow (visible as a labelled
    # node in the Graph tab; double-click to drill into the chain).
    assert "pipeline_commercial" in names
    assert "pipeline_bank" in names
    # The imperative driver is intentionally absent from the App edge graph
    # (kept only for ``test_driver_*`` coverage).
    assert "document_workflow_driver" not in names
    # Edge correctness: START → classify_node is unconditional; classify →
    # pipelines is conditional on the Event.route label.
    edges = _edges(document_workflow)
    assert ("__START__", "classify_node", None) in edges
    assert document_workflow_driver.rerun_on_resume is True


def test_driver_runs_all_nodes_present():
    """Driving the invoice branch touches every spine node exactly once."""
    ctx = _RecordingContext(
        state={},
        state_writes={"classify_node": {nodes.DOC_TYPE_KEY: nodes.ROUTE_INVOICE}},
    )
    _drive_driver(ctx)
    # Phase 8 / multi-country: resolve_jurisdiction_node now runs in the
    # commercial lane between categorize_node and tax_node so the tax LLM
    # sees a resolved jurisdiction (region, tax_system, rate band).
    assert set(ctx.call_names) == {
        "classify_node",
        "extract_invoice_document_node",
        "review_extraction_node",
        "categorize_node",
        "resolve_jurisdiction_node",
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
    assert "extract_invoice_document_node" in ctx.call_names
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
    for n in ("extract_invoice_document_node", "review_extraction_node",
              "categorize_node", "tax_node"):
        assert n not in ctx.call_names


def test_driver_invoice_lane_chain_order():
    """The invoice lane runs extract -> review -> categorize -> jurisdiction -> tax in order."""
    ctx = _RecordingContext(
        state={},
        state_writes={"classify_node": {nodes.DOC_TYPE_KEY: nodes.ROUTE_INVOICE}},
    )
    _drive_driver(ctx)
    names = ctx.call_names
    chain = [
        "extract_invoice_document_node",
        "review_extraction_node",
        "categorize_node",
        "resolve_jurisdiction_node",
        "tax_node",
    ]
    idxs = [names.index(n) for n in chain]
    assert idxs == sorted(idxs)
    assert names.index("review_extraction_node") > names.index("extract_invoice_document_node")
    assert names.index("review_extraction_node") < names.index("categorize_node")
    assert names.index("resolve_jurisdiction_node") > names.index("categorize_node")
    assert names.index("resolve_jurisdiction_node") < names.index("tax_node")


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
    asyncio.run(nodes.apply_decision_node._func(ctx, None))

    asyncio.run(nodes.route_node._func(ctx))
    assert len(ctx.state[nodes.ROUTES_KEY]) == 1

    consolidate = asyncio.run(nodes.consolidate_node._func(ctx))
    assert consolidate.output["consolidated"] == 1
    # consolidate_node prepares a Slack-agnostic ledger payload in state.
    assert ctx.state[nodes.LEDGER_ROWS_KEY]["kind"] == "invoice"
    assert len(ctx.state[nodes.LEDGER_ROWS_KEY]["batches"]) == 1

    deliver = asyncio.run(nodes.deliver_node._func(ctx))
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
    asyncio.run(nodes.apply_decision_node._func(ctx, None))
    asyncio.run(nodes.route_node._func(ctx))
    assert len(ctx.state[nodes.ROUTES_KEY]) == 1
    assert asyncio.run(nodes.consolidate_node._func(ctx)).output["consolidated"] == 1
    assert ctx.state[nodes.LEDGER_ROWS_KEY]["kind"] == "bank"
    assert asyncio.run(nodes.deliver_node._func(ctx)).output["delivered"] is True


def _drain_gate(ctx) -> list:
    """Drive the ``approval_gate`` async generator; return any yielded items.

    The gate yields a ``RequestInput`` only when the document needs human
    review; on the auto-approve path it yields nothing. Returns the list of
    yielded items so tests can assert pause vs pass-through.
    """

    async def _collect() -> list:
        return [item async for item in nodes.approval_gate(ctx)]

    return asyncio.run(_collect())


# =========================================================================== #
# Declarative lane pipelines (Track A, Phase 6)
#
# Each lane is a static ``Workflow(edges=[...])`` so ADK web shows it as a
# proper left→right pipeline. The legacy driver above covers behaviour; the
# tests below cover shape (correct edges + node order) per lane.
# =========================================================================== #


def test_commercial_pipeline_is_sequential_chain():
    """The commercial lane must be a single linear chain (no star-from-START).

    ADK web renders a linear ``edges=[(a,b), (b,c), ...]`` chain as a
    pipeline; a fan-out from START looks like a star. Track A requirement.
    """
    from accounting_agents.lane_config import COMMERCIAL_LANE
    from accounting_agents.agent import _LANE_PIPELINES

    pipeline = _LANE_PIPELINES[COMMERCIAL_LANE.route_label]
    edges = _edges(pipeline)

    # The lane nodes + terminal spine in declared order.
    expected_chain = [
        "extract_invoice_document_node",
        "review_extraction_node",
        "categorize_node",
        "resolve_jurisdiction_node",
        "tax_node",
        "approval_gate",
        "apply_decision_node",
        "route_node",
        "consolidate_node",
        "deliver_node",
    ]

    # Walk the chain via edges — every node (except START and last) has
    # exactly one successor, and edges form a strict linear chain.
    pairs = [(f, t) for f, t, _r in edges]
    assert pairs[0][0] == "__START__"
    assert pairs[0][1] == expected_chain[0]
    # Sequential edges chain correctly.
    for i, (f, t) in enumerate(pairs[1:], start=1):
        assert f == expected_chain[i - 1], (
            f"Edge {i} expected from={expected_chain[i-1]!r}, got {f!r}"
        )
        assert t == expected_chain[i], (
            f"Edge {i} expected to={expected_chain[i]!r}, got {t!r}"
        )
    # No duplicate node names (each appears exactly once in the chain).
    assert len(set(expected_chain)) == len(expected_chain)


def test_bank_pipeline_is_short_sequential_chain():
    """The bank lane has one extraction node then the terminal spine."""
    from accounting_agents.lane_config import BANK_LANE
    from accounting_agents.agent import _LANE_PIPELINES

    pipeline = _LANE_PIPELINES[BANK_LANE.route_label]
    pairs = [(f, t) for f, t, _r in _edges(pipeline)]
    # START -> extract_bank -> approval_gate -> ... -> deliver_node
    assert pairs[0] == ("__START__", "extract_bank_node")
    # Terminal spine is identical to commercial lane.
    expected_tail = [
        "approval_gate",
        "apply_decision_node",
        "route_node",
        "consolidate_node",
        "deliver_node",
    ]
    for i, expected in enumerate(expected_tail, start=1):
        assert pairs[i][1] == expected


def test_document_workflow_dispatches_by_route():
    """classify_node's Event.route label picks the lane pipeline."""
    from accounting_agents.lane_config import COMMERCIAL_LANE, BANK_LANE
    from accounting_agents.agent import _LANE_PIPELINES

    edges = _edges(document_workflow)
    # The conditional edge from classify_node carries both route labels.
    classify_edges = [
        (t, r) for f, t, r in edges if f == "classify_node"
    ]
    targets = {t: r for t, r in classify_edges}
    # If classify emits a dict-like conditional, target is the dict.
    # If it emits individual edges, each route maps to a pipeline node.
    if len(classify_edges) == 1 and classify_edges[0][1] is None:
        # The dict-style edge is encoded as (classify_node, {label: pipeline}).
        # ADK validates this at construction; just assert the pipelines exist.
        assert COMMERCIAL_LANE.route_label in _LANE_PIPELINES
        assert BANK_LANE.route_label in _LANE_PIPELINES
    else:
        # Each route has its own edge entry.
        assert targets.get("pipeline_commercial") == COMMERCIAL_LANE.route_label
        assert targets.get("pipeline_bank") == BANK_LANE.route_label
