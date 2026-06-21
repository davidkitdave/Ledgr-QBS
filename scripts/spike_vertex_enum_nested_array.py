#!/usr/bin/env python3
"""WS-0.3 spike: enum-in-nested-array on Vertex gemini-2.5-flash-lite (prod path).

Replicates research §9 Spike A on the Vertex backend (not AI Studio). Uses synthetic
COA codes only — never real client keys or vendor names.

Usage::

    uv run python scripts/spike_vertex_enum_nested_array.py
    uv run python scripts/spike_vertex_enum_nested_array.py --runs 3 --enum-size 24

Requires GOOGLE_CLOUD_PROJECT (or PROJECT_ID) + ADC. Forces GOOGLE_GENAI_USE_VERTEXAI=TRUE.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

load_dotenv(_REPO / ".env")

from google.genai import types  # noqa: E402

from invoice_processing.shared_libraries.genai_client import lite_model, make_client  # noqa: E402

ADVERSARIAL_DESCRIPTIONS: tuple[str, ...] = (
    "office rent",
    "staff salary",
    "unicorn transport reimbursement",
    "cloud hosting",
    "motor parts supply",
    "unknown miscellaneous",
)

DEFAULT_RUNS = 18
DEFAULT_ENUM_SIZE = 158


def synthetic_coa_codes(count: int) -> list[str]:
    """Generic codes 100-001 .. 100-{count:03d} — no real client data."""
    return [f"100-{i:03d}" for i in range(1, count + 1)]


def build_response_schema(valid_codes: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "account_code": {
                            "type": "string",
                            "enum": valid_codes,
                        },
                    },
                    "required": ["description", "account_code"],
                },
            }
        },
        "required": ["lines"],
    }


def build_prompt(descriptions: tuple[str, ...]) -> str:
    numbered = "\n".join(f"{i + 1}. {desc}" for i, desc in enumerate(descriptions))
    return (
        "You are an accounting assistant. For each invoice line description below, "
        "pick the single best-matching account_code from the schema enum. "
        "Return exactly one object per input line in the same order.\n\n"
        f"Line descriptions:\n{numbered}\n"
    )


def check_vertex_ready() -> str | None:
    """Return an error message if Vertex cannot run, else None."""
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
    project = os.getenv("PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project:
        return "BLOCKED: GOOGLE_CLOUD_PROJECT or PROJECT_ID not set"
    try:
        import google.auth

        google.auth.default()
    except Exception as exc:  # noqa: BLE001
        return f"BLOCKED: ADC unavailable ({exc})"
    return None


def run_spike(*, runs: int, enum_size: int) -> dict:
    valid_codes = synthetic_coa_codes(enum_size)
    valid_set = set(valid_codes)
    schema = build_response_schema(valid_codes)
    prompt = build_prompt(ADVERSARIAL_DESCRIPTIONS)
    model = lite_model()
    location = os.getenv("LOCATION", "asia-southeast1")
    project = os.getenv("PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")

    client = make_client(project=project, location=location)

    out_of_set: list[dict] = []
    api_errors: list[dict] = []
    total_lines = 0
    t0 = time.perf_counter()

    for run_idx in range(1, runs + 1):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0,
                ),
            )
            data = json.loads(resp.text or "{}")
        except Exception as exc:  # noqa: BLE001
            api_errors.append({"run": run_idx, "error": str(exc)})
            continue

        for line_idx, row in enumerate(data.get("lines") or []):
            total_lines += 1
            code = row.get("account_code")
            if code not in valid_set:
                out_of_set.append(
                    {
                        "run": run_idx,
                        "line_index": line_idx,
                        "description": row.get("description"),
                        "account_code": code,
                    }
                )

    elapsed = time.perf_counter() - t0
    expected_lines = runs * len(ADVERSARIAL_DESCRIPTIONS)
    return {
        "backend": "vertex",
        "project": project,
        "location": location,
        "model": model,
        "enum_size": enum_size,
        "runs": runs,
        "descriptions_per_run": len(ADVERSARIAL_DESCRIPTIONS),
        "expected_lines": expected_lines,
        "parsed_lines": total_lines,
        "out_of_set_count": len(out_of_set),
        "out_of_set_samples": out_of_set[:10],
        "api_error_count": len(api_errors),
        "api_error_samples": api_errors[:5],
        "elapsed_seconds": round(elapsed, 2),
        "structural_pass": len(out_of_set) == 0 and len(api_errors) == 0,
    }


def print_summary(result: dict) -> None:
    status = "PASS" if result.get("structural_pass") else "FAIL"
    print(f"\n=== WS-0.3 Vertex enum-in-nested-array spike: {status} ===")
    print(f"Backend:   Vertex ({result.get('project')} @ {result.get('location')})")
    print(f"Model:     {result.get('model')}")
    print(f"Enum size: {result.get('enum_size')} synthetic codes (100-001 …)")
    print(f"Runs:      {result.get('runs')} × {result.get('descriptions_per_run')} lines")
    print(f"Out-of-set emissions: {result.get('out_of_set_count')} / {result.get('expected_lines')}")
    if result.get("api_error_count"):
        print(f"API errors: {result.get('api_error_count')} (see samples below)")
        for sample in result.get("api_error_samples") or []:
            print(f"  run {sample['run']}: {sample['error'][:200]}")
    if result.get("out_of_set_samples"):
        print("Sample out-of-set rows:")
        for row in result["out_of_set_samples"]:
            print(f"  run {row['run']} line {row['line_index']}: {row['account_code']!r}")
    print(f"Elapsed: {result.get('elapsed_seconds')}s")
    print("Decision: STRUCTURAL CONFIRMED" if result.get("structural_pass") else "DEVIATION — review COA design")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help="Number of API runs")
    parser.add_argument(
        "--enum-size",
        type=int,
        default=DEFAULT_ENUM_SIZE,
        help="Count of synthetic enum keys (default 158)",
    )
    args = parser.parse_args()

    blocked = check_vertex_ready()
    if blocked:
        print(blocked)
        print("\n=== WS-0.3 Vertex enum-in-nested-array spike: BLOCKED ===")
        return 2

    result = run_spike(runs=args.runs, enum_size=args.enum_size)
    print_summary(result)
    return 0 if result["structural_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
