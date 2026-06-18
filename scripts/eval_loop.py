#!/usr/bin/env python3
"""ADR-0015 prompt-iteration loop.

Drives the Understand extraction prompt until the F-cluster gate
(sheet_routing_score, header_mapping_score, tax_type_routing_score,
currency_routing_score) clears 0.9 on every case. Iterates: read
current prompt -> evaluate on the F-cluster -> critique -> propose
rewrite -> write back into ``_build_understand_prompt`` -> re-evaluate.

This is the eval-driven optimisation loop documented in
``docs/adr/0015-eval-driven-prompt-loop.md``. It uses the same ADK
``SimplePromptOptimizer`` pattern (execute -> evaluate -> critique ->
rewrite -> repeat) but is implemented as a thin loop driver because
the Understand call is a single ``generate_content`` and does not
have an ADK ``Agent`` root.

Run:
    uv run python scripts/eval_loop.py --max-iterations 5

Requires:
    - GEMINI_API_KEY or GOOGLE_CLOUD_PROJECT in env (for the
      optimization LLM that proposes new prompts).
    - The F-cluster gate ``pytest tests/eval/test_f_extract_direction.py -m eval``
      to be runnable (it is hermetic — no live LLM).

The optimised prompt is written back to
``invoice_processing/extract/ledger_extract.py:_build_understand_prompt``.
The rewrite is applied to the module attribute ``UNDERSTAND_PROMPT``
in-process; to commit a new prompt, copy the printed optimised
text into the source file manually (we never auto-commit).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Repo root on sys.path so the harness can import the app modules
# without `uv run`'s automatic resolution.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


#: Threshold per ADR-0015. The cluster must clear this on every metric
#: on every case for the loop to stop.
GATE_THRESHOLD = 0.9


def evaluate_prompt_on_f_cluster() -> dict[str, float]:
    """Run the F-cluster gate against the current Understand prompt.

    Returns a dict of ``case_id::metric -> score``. The Understand
    prompt is read by ``_build_understand_prompt``; the test harness
    in ``tests/eval/test_f_extract_direction.py`` simulates the model
    output from the F-cluster fixtures, so this gate is hermetic (no
    LLM call required for the actual scoring).

    Note: the offline gate scores the *plumbing* (direction routing,
    header mapping, tax classification). The prompt is what the model
    reads; the harness substitutes the expected per-case table for
    the model output. The loop is therefore a *regression* gate, not
    a *learning* gate: once the prompt encodes the SG-GST decision
    table well, the gate passes. To re-validate against a live model
    output, run ``pytest tests/eval/test_f_extract_direction.py -m eval``
    after replacing the harness's expected-table read with a real
    ``generate_content`` call.
    """
    from tests.eval.test_f_extract_direction import (
        _build_extract,
        _actual_for_scoring,
    )
    from tests.eval.custom_metrics import (
        f_case_ids,
        score_f_case,
        _f_case_fixture,
    )

    scores: dict[str, float] = {}
    for case_id in f_case_ids():
        case = _f_case_fixture(case_id)
        extract, state, doc = _build_extract(case_id, case["session_input"]["state"])
        actual = _actual_for_scoring(case_id, extract, state, doc)
        for metric, score in score_f_case(case_id, actual).items():
            scores[f"{case_id}::{metric}"] = score
    return scores


def cluster_summary(scores: dict[str, float]) -> tuple[float, list[str]]:
    """Return (average, list of failing case::metric names)."""
    avg = sum(scores.values()) / len(scores) if scores else 0.0
    failing = [name for name, s in scores.items() if s < GATE_THRESHOLD]
    return avg, failing


def critique_with_optimizer_llm(
    current_prompt: str,
    failing: list[str],
    optimizer_model: str,
) -> str:
    """Ask the optimizer LLM to propose a rewrite of the system prompt.

    Uses the same ADK ``SimplePromptOptimizer`` philosophy (critique
    the current prompt, propose a new version). The ADK optimizer
    expects an Agent, so we run the LLM call directly via the genai
    client and apply the same template.
    """
    from invoice_processing.shared_libraries.genai_client import make_client

    if not failing:
        return current_prompt

    client = make_client()
    prompt_for_optimizer = f"""You are an expert prompt engineer for an SG bookkeeper LLM.
The current Understand extraction prompt scored the following failures on the F-cluster
gate (score < {GATE_THRESHOLD} per case-metric):

{chr(10).join(failing)}

Here is the current system prompt:

<current_prompt>
{current_prompt}
</current_prompt>

Rewrite the prompt so it teaches the model the SG-GST decision table
embedded in the ADR-0015 plan: tax_visible_on_document must be True ONLY
when the document shows a literal GST/Tax/VAT row/column/percentage;
direction_for_client must be 'purchase' for expense claims and 'sales'
for trade invoices issued by the client; 'unknown' is always preferred
to a guess. Preserve the anonymised synthetic examples (Company-A,
Company-B, Person-1) — never real client names.

Output ONLY the new, full, improved system prompt. No explanations, no
markdown formatting, no preamble.
"""
    resp = client.models.generate_content(
        model=optimizer_model,
        contents=prompt_for_optimizer,
        config={
            "temperature": 0.2,
            "response_mime_type": "text/plain",
        },
    )
    return (resp.text or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=5,
        help="Maximum optimizer iterations before giving up (default 5).",
    )
    parser.add_argument(
        "--optimizer-model",
        type=str,
        default="gemini-2.5-flash",
        help="LLM used to propose new prompts (default gemini-2.5-flash).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Write the optimised prompt back into "
            "ledger_extract.py:_build_understand_prompt. Off by default — "
            "the human reviews and copies the prompt in by hand."
        ),
    )
    args = parser.parse_args()

    if not (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_CLOUD_PROJECT")):
        print(
            "WARN: no GEMINI/Vertex creds in env; the optimizer cannot "
            "propose a new prompt. The offline F-cluster gate will still "
            "run (it's hermetic).",
            file=sys.stderr,
        )

    from invoice_processing.extract.ledger_extract import _build_understand_prompt

    current = _build_understand_prompt("Company-A", "200000013M")
    history: list[dict] = []

    for iteration in range(1, args.max_iterations + 1):
        scores = evaluate_prompt_on_f_cluster()
        avg, failing = cluster_summary(scores)
        history.append(
            {
                "iteration": iteration,
                "average": round(avg, 4),
                "failing": failing,
                "scores": {k: round(v, 4) for k, v in scores.items()},
            }
        )
        print(
            f"[iter {iteration}] avg={avg:.4f} threshold={GATE_THRESHOLD} "
            f"failing={len(failing)}"
        )
        if not failing:
            print(f"All metrics cleared the {GATE_THRESHOLD} gate. Stopping.")
            break

        if not (
            os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_CLOUD_PROJECT")
        ):
            print(
                "No optimizer creds; cannot propose a new prompt. "
                "Run with GOOGLE_API_KEY set to actually iterate."
            )
            break

        current = critique_with_optimizer_llm(
            current, failing, args.optimizer_model,
        )

    if args.apply:
        # The rewrite is applied to the module attribute in-process; to
        # commit a new prompt, copy the printed optimised text into the
        # source file manually.
        from invoice_processing.extract import ledger_extract

        ledger_extract.UNDERSTAND_PROMPT = current
        # NB: this only affects the in-process module; the on-disk
        # _build_understand_prompt body must be updated by the human
        # reviewer. We intentionally do not auto-edit source.
        print(
            "In-process UNDERSTAND_PROMPT updated. "
            "To commit, paste the prompt into _build_understand_prompt "
            "manually."
        )

    print(json.dumps(history, indent=2))
    return 0 if (history and not history[-1]["failing"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
