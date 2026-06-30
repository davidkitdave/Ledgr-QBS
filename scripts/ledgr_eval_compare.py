#!/usr/bin/env python3
"""Diff two per-case/per-metric ledgr light eval result JSON files."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _case_metrics(data: dict) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for case in data.get("cases") or []:
        case_id = case.get("eval_case_id") or case.get("eval_id") or "unknown"
        metrics = {}
        for name, value in (case.get("metrics") or {}).items():
            if isinstance(value, dict):
                metrics[name] = float(value.get("score", value.get("overall_score", 0.0)))
            else:
                metrics[name] = float(value)
        out[str(case_id)] = metrics
    return out


def compare(baseline: dict, current: dict) -> list[str]:
    base_cases = _case_metrics(baseline)
    curr_cases = _case_metrics(current)
    lines: list[str] = []
    all_case_ids = sorted(set(base_cases) | set(curr_cases))
    for case_id in all_case_ids:
        base_m = base_cases.get(case_id, {})
        curr_m = curr_cases.get(case_id, {})
        all_metrics = sorted(set(base_m) | set(curr_m))
        for metric in all_metrics:
            before = base_m.get(metric)
            after = curr_m.get(metric)
            if before is None and after is None:
                continue
            delta = (after or 0.0) - (before or 0.0)
            sign = "+" if delta >= 0 else ""
            lines.append(
                f"{case_id}\t{metric}\t{before}\t{after}\t{sign}{delta:.3f}"
            )
    overall_before = baseline.get("overall_mean")
    overall_after = current.get("overall_mean")
    if overall_before is not None or overall_after is not None:
        delta = (overall_after or 0.0) - (overall_before or 0.0)
        sign = "+" if delta >= 0 else ""
        lines.append(
            f"OVERALL\tmean\t{overall_before}\t{overall_after}\t{sign}{delta:.3f}"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=pathlib.Path)
    parser.add_argument("current", type=pathlib.Path)
    args = parser.parse_args(argv)
    baseline = _load(args.baseline)
    current = _load(args.current)
    print("case_id\tmetric\tbefore\tafter\tdelta")
    for line in compare(baseline, current):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
