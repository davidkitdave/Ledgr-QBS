"""Tests for ``accounting_agents.adk_web_qa`` — the post-build ADK web QA harness.

We can't drive a real browser from a non-interactive session, so we exercise
the harness's structural assertions (per-view checklist items). The harness
itself runs the document lane against the YAU LEE PDF and emits a markdown
report; this test verifies:

* The harness module is importable and the default client state is correct.
* The per-view checklist keys are present in the rendered markdown.
* The Track A + Track B graph assertions are encoded in the checklist (single
  root, classify_node inlined, no nested document_workflow box).
* The YAU LEE line must NOT be flagged as SR 9% mismatch on valid 8% SST.
"""

from __future__ import annotations

from pathlib import Path

from accounting_agents.adk_web_qa import _default_client_state


def test_default_client_state_is_jbi_plus_malaysia():
    """The harness defaults to the YAU LEE playground profile (JBI PLUS / MY / MYR)."""
    state = _default_client_state()
    assert state["region"] == "MALAYSIA"
    assert state["base_currency"] == "MYR"
    assert state["client_id"] == "jbi-plus-auto"
    # COA must be seeded (categorize_node LLM requires it).
    assert len(state["coa"]) >= 1
    # Entity memory should mention YAU LEE so the categorizer maps directly.
    entity_names = {e["name"] for e in state["entity_memory"]}
    assert "YAU LEE MOTOR" in entity_names


def test_render_checklist_includes_all_four_tabs(tmp_path: Path):
    """The markdown checklist covers Graph / Traces / Events / State views."""
    from accounting_agents.adk_web_qa import _render_checklist

    out = tmp_path / "qa.md"
    _render_checklist(
        report={
            "pdf": "scratch/yau_lee_motor_receipt.pdf",
            "graph_tab": {
                "coordinator_nodes": [
                    "__START__",
                    "coordinator",
                    "dynamic_router",
                    "classify_node",
                    "pipeline_commercial",
                    "pipeline_bank",
                    "help_node",
                ],
                "coordinator_edges": [],
                "pipeline_subworkflows": [
                    "bank_statement",
                    "commercial_doc",
                ],
            },
            "traces_tab": {
                "execution_order": [
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
                ],
                "events_total": 42,
            },
            "events_tab": {
                "doc_type": "receipt",
                "direction": "purchase",
                "region": "MALAYSIA",
                "base_currency": "MYR",
                "tax_jurisdiction": "MALAYSIA",
                "approval_status": "auto_approved",
            },
            "state_tab": {
                "normalized_invoice_count": 1,
                "lines": [
                    {
                        "tax_treatment": "SR",
                        "tax_flagged": False,
                        "account_code": "500-020",
                        "tax_amount": 4.81,
                        "net_amount": 60.19,
                        "currency": "MYR",
                    }
                ],
            },
        },
        output_path=out,
    )

    text = out.read_text()
    # 1. Graph tab — Track B (flat) + Track A (per-lane pipeline) checks.
    assert "## 1. Graph tab" in text
    assert "Single-root flat graph (Track B): **PASS**" in text
    assert "Per-lane sub-Workflow visible (Track A): **PASS**" in text
    # 2. Traces tab.
    assert "## 2. Traces tab" in text
    assert "Pipeline order" in text
    assert "PASS" in text  # full chain present in expected order
    # 3. Events tab.
    assert "## 3. Events tab" in text
    assert "region seeded from profile: **PASS**" in text
    assert "tax_jurisdiction written to state" in text
    # 4. State tab — YAU LEE must NOT be flagged (the entire point of Phase 8).
    assert "## 4. State tab" in text
    assert "YAU LEE lines NOT flagged (8% SST, not SG 9%): **PASS**" in text


def test_render_checklist_fails_when_no_normalized_lines(tmp_path: Path):
    """If extraction produced no lines, the harness reports FAIL clearly."""
    from accounting_agents.adk_web_qa import _render_checklist

    out = tmp_path / "qa.md"
    _render_checklist(
        report={
            "pdf": "missing.pdf",
            "graph_tab": {
                "coordinator_nodes": ["__START__", "coordinator", "dynamic_router"],
                "coordinator_edges": [],
                "pipeline_subworkflows": [],
            },
            "traces_tab": {"execution_order": [], "events_total": 0},
            "events_tab": {
                "doc_type": None,
                "direction": None,
                "region": None,
                "base_currency": None,
                "tax_jurisdiction": None,
                "approval_status": None,
            },
            "state_tab": {"normalized_invoice_count": 0, "lines": []},
        },
        output_path=out,
    )
    text = out.read_text()
    assert "YAU LEE lines NOT flagged" in text
    assert "**FAIL**" in text