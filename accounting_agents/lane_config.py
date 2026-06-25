"""Lane registry — single source of truth for the document workflow's per-doc-type lanes.

Phase 2 / lane-registry task. The previous driver hard-coded an
``if doc_type == "bank_statement": ... else: ...`` branch in
``document_workflow_driver``. The class was renamed from ``invoice`` to
``commercial_doc`` in traces to align with the model's ``doc_type`` vocabulary
(receipt / invoice / etc.) — but that was a band-aid; the real fix is one
declarative map.

Design:

* :data:`DOC_TYPE_TO_LANE` is consulted by both ``classify_node`` (to pick
  the right ``Event(route=...)`` label) and ``document_workflow_driver``
  (to iterate the lane's node list). When the two consumers disagree, the
  trace stops telling the truth — which was a past MY review's "route
  invoice vs doc_type receipt" gap.

* Adding a new doc type (e.g. ``delivery_order``) is now ONE entry in
  :data:`DOC_TYPE_TO_LANE` — no scattered Python ``if/else``.

* Lane node lists contain ``@node`` callables. The driver iterates them in
  order and dispatches via ``ctx.run_node`` (so HITL resume still replays
  the driver top-down and checkpointed sub-nodes dedup correctly).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

# Lane labels (match ``nodes.ROUTE_*``; centralised here so the registry is
# the only place these strings are defined for the document lane).
ROUTE_COMMERCIAL_DOC = "commercial_doc"
ROUTE_BANK = "bank_statement"

# Stable names exported so tests + downstream nodes can import them.
LANE_BANK = "bank"
LANE_COMMERCIAL = "commercial"


@dataclass
class LaneSpec:
    """Declarative description of one document-processing lane."""

    name: str
    route_label: str
    node_names: tuple[str, ...]
    description: str = ""

    def node_factories(self) -> dict[str, Callable]:
        """Resolve node names to callables via :func:`get_node_factory`."""
        return {n: get_node_factory(n) for n in self.node_names}


# --------------------------------------------------------------------------- #
# Lane definitions — keep node order matching the previous static driver.
# --------------------------------------------------------------------------- #
BANK_LANE = LaneSpec(
    name=LANE_BANK,
    route_label=ROUTE_BANK,
    node_names=("extract_bank",),
    description="Bank-statement extraction lane (single vision/text node).",
)

COMMERCIAL_LANE = LaneSpec(
    name=LANE_COMMERCIAL,
    route_label=ROUTE_COMMERCIAL_DOC,
    node_names=(
        "extract_invoice",
        "review_extraction",
        "categorize",
        "resolve_jurisdiction",
        "tax",
    ),
    description=(
        "Commercial doc (invoice / receipt) lane: understand-extract → review → "
        "categorize → resolve jurisdiction → tax."
    ),
)


# --------------------------------------------------------------------------- #
# DOC_TYPE → LANE map (single source of truth)
# --------------------------------------------------------------------------- #
DOC_TYPE_TO_LANE: dict[str, LaneSpec] = {
    "bank_statement": BANK_LANE,
    "invoice": COMMERCIAL_LANE,
    "receipt": COMMERCIAL_LANE,
    "tax_invoice": COMMERCIAL_LANE,
    "delivery_order": COMMERCIAL_LANE,
    "other": COMMERCIAL_LANE,  # generic fallback (post ADR-0017 §2 clamp)
}


def lane_for_doc_type(doc_type: str) -> LaneSpec:
    """Return the LaneSpec for a ``doc_type`` string. Defaults to COMMERCIAL."""
    if not doc_type:
        return COMMERCIAL_LANE
    return DOC_TYPE_TO_LANE.get(doc_type.strip().lower(), COMMERCIAL_LANE)


def route_for_doc_type(doc_type: str) -> str:
    """Return the canonical route label for a ``doc_type`` string."""
    return lane_for_doc_type(doc_type).route_label


# --------------------------------------------------------------------------- #
# Node factory — resolves symbolic names to the actual @node callables.
# Lazy-loaded on first call so importing this module never requires ADK
# graph internals.
# --------------------------------------------------------------------------- #
_NODE_FACTORIES: Optional[dict[str, Callable]] = None


def _build_node_factories() -> dict[str, Callable]:
    from . import nodes  # late import — keeps top-of-file safe

    return {
        "extract_bank": nodes.extract_bank_node,
        "extract_invoice": nodes.extract_invoice_document_node,
        "review_extraction": nodes.review_extraction_node,
        "categorize": nodes.categorize_node,
        "resolve_jurisdiction": nodes.resolve_jurisdiction_node,
        "tax": nodes.tax_node,
    }


def get_node_factory(name: str) -> Callable:
    """Return the ``@node`` callable for ``name``.

    Raises KeyError with a clear message if the symbolic name is unknown —
    catches typos at driver-construction time rather than at run time.
    """
    global _NODE_FACTORIES
    if _NODE_FACTORIES is None:
        _NODE_FACTORIES = _build_node_factories()
    if name not in _NODE_FACTORIES:
        raise KeyError(
            f"Unknown lane node name: {name!r}. "
            f"Known names: {sorted(_NODE_FACTORIES)}"
        )
    return _NODE_FACTORIES[name]


def clear_node_factory_cache() -> None:
    """Reset the node-factory cache (test helper)."""
    global _NODE_FACTORIES
    _NODE_FACTORIES = None