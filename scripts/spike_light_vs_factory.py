#!/usr/bin/env python3
"""A/B race: light LLM path vs heavy factory on real documents.

Round 1 starts as ONE generic Gemini call (no rules). Use ``--round`` or
``--auto-climb`` to add tax / COA / export rules only when mismatches prove
they are needed.

Usage::

    set -a && source .env && set +a
    uv run python scripts/spike_light_vs_factory.py --all-fixtures
    uv run python scripts/spike_light_vs_factory.py --round 1 --pdf path/to/bill.pdf
    uv run python scripts/spike_light_vs_factory.py --auto-climb --all-fixtures
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO / ".env")

from ledgr_agent.tools.light_ledger import LightRound, light_process  # noqa: E402

DEFAULT_OUT_DIR = Path("/Users/davidkitdave/Desktop/localtest")

# One fixture per doc kind — only paths that exist on disk are run.
DEFAULT_FIXTURES: list[dict[str, str]] = [
    {
        "label": "bill",
        "path": (
            "/Users/davidkitdave/Desktop/localtest/TestDoc/GST SR:ZR/"
            "BV-0002830 Starhub 8.20057598B bill 122025.pdf"
        ),
    },
    {
        "label": "receipt",
        "path": "/Users/davidkitdave/Desktop/localtest/multi receipt.pdf",
    },
    {
        "label": "bank_statement",
        "path": (
            "/Users/davidkitdave/Desktop/localtest/TestDoc/Cast Unity/"
            "Orange Perspective Consulting Pte. Ltd./BankStatement/FY2025/"
            "Oct2025_MariBank_Business_e-Statement.pdf"
        ),
    },
    {
        "label": "credit_note",
        "path": (
            "/Users/davidkitdave/Desktop/localtest/TestDoc/Cast Unity/"
            "DMTV Global Pte Ltd/Sales/FY2025/dMTV - Delfin - credit note 001.pdf"
        ),
    },
    {
        "label": "expense_claim",
        "path": (
            "/Users/davidkitdave/Desktop/localtest/TestDoc/Cast Unity/"
            "Auditair Helideck Certification Pte. Ltd./Purchase/FY2026/"
            "Naufal Expense Claim-AHC-25-026.pdf"
        ),
    },
]


def _playground_state() -> dict:
    from invoice_processing.shared_libraries.playground_context import playground_default_context

    state = playground_default_context().to_state()
    state.setdefault("firm_id", "T_PLAYGROUND")
    state.setdefault("slack_team_id", "T_PLAYGROUND")
    return state


def run_factory(pdf: Path, *, credits_grant: int = 500) -> dict[str, Any]:
    from app.credit_service import CreditService, InMemoryCreditStore
    from ledgr_agent.tools import document_tools
    from ledgr_agent.tools.document_tools import process_document_batch

    service = CreditService(InMemoryCreditStore())
    document_tools._credit_service_factory = lambda: service
    service.grant("T_PLAYGROUND", credits_grant, note="spike-light-vs-factory")

    t0 = time.perf_counter()
    batch = process_document_batch(
        SimpleNamespace(state=_playground_state()),
        paths=[str(pdf.resolve())],
    )
    elapsed = time.perf_counter() - t0

    export_rows = batch.get("export_rows") or []
    posted = batch.get("posted_documents") or []
    per_file = batch.get("per_file") or []
    validation = batch.get("validation_summary") or {}

    return {
        "elapsed_seconds": round(elapsed, 2),
        "batch_status": batch.get("status"),
        "block_reason": validation.get("block_reason"),
        "posted_document_count": len(posted),
        "per_file_count": len(per_file),
        "per_file_doc_types": [
            pf.get("doc_type") for pf in per_file if isinstance(pf, dict)
        ],
        "export_row_count": len(export_rows),
        "export_rows": export_rows,
        "posted_documents": posted,
        "per_file": per_file,
        "documents_processed": batch.get("documents_processed"),
        "llm_call_count": validation.get("llm_call_count"),
    }


def _norm_tax(code: object) -> str | None:
    if code is None:
        return None
    text = str(code).strip().upper()
    return text or None


def _norm_account(code: object) -> str | None:
    if code is None:
        return None
    text = str(code).strip()
    return text or None


def _tax_list_from_rows(rows: list[dict]) -> list[str]:
    out: list[str] = []
    for row in rows:
        tax = _norm_tax(row.get("tax_treatment"))
        if tax:
            out.append(tax)
    return sorted(out)


def _account_list_from_rows(rows: list[dict]) -> list[str]:
    out: list[str] = []
    for row in rows:
        code = _norm_account(row.get("account_code"))
        if code:
            out.append(code)
    return sorted(out)


def _grand_total_from_light(light: dict[str, Any]) -> float | None:
    docs = (light.get("bundle") or {}).get("documents") or []
    if not docs:
        return None
    totals = [d.get("grand_total") for d in docs if isinstance(d, dict)]
    nums = [float(t) for t in totals if t is not None]
    if not nums:
        return None
    return round(sum(nums), 2)


def _grand_total_from_factory(factory: dict[str, Any]) -> float | None:
    rows = factory.get("export_rows") or []
    if not rows:
        posted = factory.get("posted_documents") or []
        for doc in posted:
            total = doc.get("total")
            if total is not None:
                return round(float(total), 2)
        return None
    total_col_keys = ("Total Amount", "Total", "total_amount", "total")
    acc = 0.0
    found = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in total_col_keys:
            val = row.get(key)
            if val not in (None, ""):
                try:
                    acc += float(val)
                    found = True
                except (TypeError, ValueError):
                    pass
                break
    return round(acc, 2) if found else None


def summarize_light(light: dict[str, Any]) -> dict[str, Any]:
    rows = light.get("export_rows") or []
    return {
        "doc_count": int(light.get("doc_count") or 0),
        "row_count": int(light.get("row_count") or len(rows)),
        "tax_line_count": int(light.get("tax_line_count") or 0),
        "tax_treatments": _tax_list_from_rows(rows),
        "account_codes": _account_list_from_rows(rows),
        "grand_total": _grand_total_from_light(light),
        "gemini_call_count": light.get("gemini_call_count"),
        "elapsed_seconds": light.get("elapsed_seconds"),
    }


def summarize_factory(factory: dict[str, Any]) -> dict[str, Any]:
    rows = factory.get("export_rows") or []
    return {
        "doc_count": int(factory.get("posted_document_count") or factory.get("per_file_count") or 0),
        "row_count": int(factory.get("export_row_count") or len(rows)),
        "tax_line_count": None,
        "tax_treatments": _tax_list_from_rows(rows),
        "account_codes": _account_list_from_rows(rows),
        "grand_total": _grand_total_from_factory(factory),
        "gemini_call_count": factory.get("llm_call_count"),
        "elapsed_seconds": factory.get("elapsed_seconds"),
    }


def compare_paths(light: dict[str, Any], factory: dict[str, Any]) -> dict[str, Any]:
    """Return match verdict + mismatches for one doc."""
    light_summary = summarize_light(light)
    factory_summary = summarize_factory(factory)
    mismatches: list[dict[str, Any]] = []
    light_beats = False

    if factory_summary["doc_count"] > light_summary["doc_count"] and light_summary["doc_count"] >= 1:
        light_beats = True

    if light_summary["row_count"] > max(factory_summary["row_count"] * 3, factory_summary["row_count"] + 5):
        mismatches.append(
            {
                "field": "row_count",
                "light": light_summary["row_count"],
                "factory": factory_summary["row_count"],
                "note": "light over-extracted detail rows",
            }
        )

    if light_summary["tax_treatments"] and factory_summary["tax_treatments"]:
        if set(light_summary["tax_treatments"]) != set(factory_summary["tax_treatments"]):
            mismatches.append(
                {
                    "field": "tax_treatment",
                    "light": light_summary["tax_treatments"],
                    "factory": factory_summary["tax_treatments"],
                }
            )
    elif factory_summary["tax_treatments"] and not light_summary["tax_treatments"]:
        mismatches.append(
            {
                "field": "tax_treatment",
                "light": light_summary["tax_treatments"],
                "factory": factory_summary["tax_treatments"],
                "note": "light missing tax codes",
            }
        )

    if factory_summary["account_codes"]:
        if not light_summary["account_codes"]:
            mismatches.append(
                {
                    "field": "account_code",
                    "light": light_summary["account_codes"],
                    "factory": factory_summary["account_codes"],
                    "note": "light missing account codes",
                }
            )
        elif set(light_summary["account_codes"]) != set(factory_summary["account_codes"]):
            mismatches.append(
                {
                    "field": "account_code",
                    "light": light_summary["account_codes"],
                    "factory": factory_summary["account_codes"],
                }
            )

    if light_summary["grand_total"] is not None and factory_summary["grand_total"] is not None:
        if abs(light_summary["grand_total"] - factory_summary["grand_total"]) > 0.05:
            mismatches.append(
                {
                    "field": "grand_total",
                    "light": light_summary["grand_total"],
                    "factory": factory_summary["grand_total"],
                }
            )

    if light_summary["elapsed_seconds"] is not None and factory_summary["elapsed_seconds"] is not None:
        if float(light_summary["elapsed_seconds"]) < float(factory_summary["elapsed_seconds"]):
            light_beats = True

    matched = len(mismatches) == 0
    return {
        "matched": matched,
        "light_beats_factory": light_beats,
        "mismatches": mismatches,
        "light_summary": light_summary,
        "factory_summary": factory_summary,
    }


def _fmt_side(label: str, summary: dict[str, Any]) -> str:
    return (
        f"  [{label}]\n"
        f"    doc_count:        {summary['doc_count']}\n"
        f"    row_count:        {summary['row_count']}\n"
        f"    tax_treatments:   {summary['tax_treatments'] or '(none)'}\n"
        f"    account_codes:    {summary['account_codes'] or '(none)'}\n"
        f"    grand_total:      {summary['grand_total']}\n"
        f"    gemini_calls:     {summary['gemini_call_count']}\n"
        f"    elapsed_seconds:  {summary['elapsed_seconds']}"
    )


def _log(msg: str) -> None:
    print(msg, flush=True)


def run_one_fixture(
    fixture: dict[str, str],
    *,
    round_num: LightRound,
    skip_factory: bool,
    out_dir: Path,
    factory_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    pdf = Path(fixture["path"]).expanduser()
    label = fixture["label"]
    if not pdf.exists():
        return {"label": label, "path": str(pdf), "skipped": True, "reason": "file not found"}

    _log("=" * 72)
    _log(f"Fixture: {label} — {pdf.name}")
    _log("=" * 72)

    _log(f"\n[Light round {round_num}] starting…")
    light = light_process(pdf, round_num=round_num)
    _log(_fmt_side("light", summarize_light(light)))

    factory: dict[str, Any] = {}
    comparison: dict[str, Any] = {"matched": None, "light_beats_factory": None, "mismatches": []}
    if not skip_factory:
        cache_key = str(pdf.resolve())
        if factory_cache is not None and cache_key in factory_cache:
            factory = factory_cache[cache_key]
            _log("\n[Factory — cached from earlier run]")
        else:
            _log("\n[Factory — process_document_batch] starting… (large PDFs can take 1–5 min)")
            try:
                factory = run_factory(pdf)
                if factory_cache is not None:
                    factory_cache[cache_key] = factory
            except Exception as exc:  # noqa: BLE001
                factory = {"error": f"{type(exc).__name__}: {exc}"}
                _log(f"  ERROR: {factory['error']}")
        if factory and "error" not in factory:
            _log(_fmt_side("factory", summarize_factory(factory)))
            comparison = compare_paths(light, factory)

    safe = pdf.stem.replace(" ", "_").replace("/", "_")[:60]
    light_path = out_dir / f"{safe}_{label}_light_r{round_num}.json"
    factory_path = out_dir / f"{safe}_{label}_factory.json"
    light_path.write_text(json.dumps(light, indent=2, default=str, ensure_ascii=False))
    if factory:
        factory_path.write_text(json.dumps(factory, indent=2, default=str, ensure_ascii=False))

    if comparison.get("mismatches"):
        _log("\n  Mismatches:")
        for mm in comparison["mismatches"]:
            _log(f"    - {mm['field']}: light={mm.get('light')} factory={mm.get('factory')}")

    _log(f"\n  -> wrote {light_path}")
    if factory and "error" not in factory:
        _log(f"  -> wrote {factory_path}")

    return {
        "label": label,
        "path": str(pdf),
        "round": round_num,
        "light": light,
        "factory": factory,
        "comparison": comparison,
        "light_path": str(light_path),
        "factory_path": str(factory_path) if factory else None,
    }


def _ladder_trigger(comparison: dict[str, Any]) -> str | None:
    for mm in comparison.get("mismatches") or []:
        field = mm.get("field")
        if field == "row_count":
            return "tax"
        if field == "tax_treatment":
            return "tax"
        if field == "account_code":
            return "coa"
        if field in {"grand_total", "route", "export"}:
            return "export"
    return None


def _round_for_trigger(trigger: str | None, current: LightRound) -> LightRound:
    if trigger == "tax" and current < 2:
        return 2
    if trigger == "coa" and current < 3:
        return 3
    if trigger == "export" and current < 4:
        return 4
    return current


def auto_climb_fixture(
    fixture: dict[str, str],
    *,
    out_dir: Path,
    factory_cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Try rounds 1→4 until mismatches clear. Factory runs once per PDF (cached)."""
    round_num: LightRound = 1
    history: list[dict[str, Any]] = []
    winning_round: LightRound | None = None

    while round_num <= 4:
        _log(f"\n>>> Ladder round {round_num} for {fixture['label']}")
        result = run_one_fixture(
            fixture,
            round_num=round_num,
            skip_factory=False,
            out_dir=out_dir,
            factory_cache=factory_cache,
        )
        if result.get("skipped"):
            return result
        comparison = result.get("comparison") or {}
        history.append(
            {
                "round": round_num,
                "matched": comparison.get("matched"),
                "mismatches": comparison.get("mismatches"),
            }
        )
        if comparison.get("matched"):
            winning_round = round_num
            break
        trigger = _ladder_trigger(comparison)
        next_round = _round_for_trigger(trigger, round_num)
        if next_round == round_num:
            next_round = min(4, round_num + 1)  # type: ignore[assignment]
        if next_round <= round_num:
            break
        round_num = next_round  # type: ignore[assignment]

    result["ladder_history"] = history
    result["winning_round"] = winning_round
    return result


def build_verdict(results: list[dict[str, Any]], *, round_num: LightRound | str) -> dict[str, Any]:
    active = [r for r in results if not r.get("skipped")]
    matched = [r for r in active if (r.get("comparison") or {}).get("matched")]
    mismatch_fields: dict[str, list[str]] = {}
    for r in active:
        for mm in (r.get("comparison") or {}).get("mismatches") or []:
            field = str(mm.get("field") or "unknown")
            mismatch_fields.setdefault(field, []).append(str(r.get("label") or "?"))

    winning_rounds = [r.get("winning_round") for r in active if r.get("winning_round")]
    proven_minimum = min(winning_rounds) if winning_rounds else None

    verdict_text = (
        f"Round {round_num} (LLM + ladder): matched factory on {len(matched)}/{len(active)} docs."
    )
    if mismatch_fields:
        parts = [f"{field} on {', '.join(labels)}" for field, labels in mismatch_fields.items()]
        verdict_text += f" Mismatches: {'; '.join(parts)}."
    if proven_minimum is not None:
        verdict_text += f" Proven minimum round: {proven_minimum}."

    return {
        "round": round_num,
        "matched_count": len(matched),
        "total_count": len(active),
        "mismatch_fields": mismatch_fields,
        "proven_minimum_round": proven_minimum,
        "verdict_text": verdict_text,
        "results": [
            {
                "label": r.get("label"),
                "matched": (r.get("comparison") or {}).get("matched"),
                "light_beats_factory": (r.get("comparison") or {}).get("light_beats_factory"),
                "winning_round": r.get("winning_round"),
                "mismatches": (r.get("comparison") or {}).get("mismatches"),
            }
            for r in active
        ],
    }


def _verdict_markdown(verdict: dict[str, Any]) -> str:
    rule_map = {
        1: "LLM alone — one generic `generate_content` + Pydantic schema",
        2: "LLM + `classify_invoice` (deterministic tax rule)",
        3: "LLM + tax + `categorize_invoice` (COA step)",
        4: "LLM + tax + COA + `route_document` + QBS export",
    }
    lines = [
        "# Light vs factory — proven minimum",
        "",
        verdict.get("verdict_text", ""),
        "",
    ]
    minimum = verdict.get("proven_minimum_round")
    if minimum is not None:
        lines.append(f"**Proven minimum:** Round {minimum} — {rule_map.get(minimum, '?')}")
        lines.append("")
    lines.append("## Per-doc results")
    lines.append("")
    for row in verdict.get("results") or []:
        status = "matched" if row.get("matched") else "mismatch"
        beats = " (light faster/cleaner)" if row.get("light_beats_factory") else ""
        win = row.get("winning_round")
        win_txt = f", winning round {win}" if win else ""
        lines.append(f"- **{row.get('label')}**: {status}{beats}{win_txt}")
        for mm in row.get("mismatches") or []:
            lines.append(f"  - {mm.get('field')}: light={mm.get('light')} factory={mm.get('factory')}")
    lines.append("")
    lines.append("## What this means")
    lines.append("")
    if minimum == 1:
        lines.append(
            "The factory over-built it for these fixtures — one LLM read is enough "
            "before adding rules."
        )
    elif minimum in {2, 3}:
        lines.append(
            f"Keep a thin rule chain through round {minimum}; drop chunking, spine, "
            "validators, and batch mapper from the factory."
        )
    elif minimum == 4:
        lines.append("Need read + tax + COA + export, but still far less than the full factory.")
    else:
        lines.append(
            "No single round matched all fixtures — see per-doc mismatches; "
            "factory rules may still be needed for some doc types."
        )
    lines.append("")
    return "\n".join(lines)


QUICK_FIXTURE_LABELS = frozenset({"bill", "credit_note", "expense_claim"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, help="Single PDF to race")
    parser.add_argument("--all-fixtures", action="store_true", help="Run all default doc-type fixtures")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Skip slow fixtures (35-page multi-receipt PDF and bank statement)",
    )
    parser.add_argument("--round", type=int, default=1, choices=[1, 2, 3, 4], help="Ladder round")
    parser.add_argument("--auto-climb", action="store_true", help="Climb rounds 1→4 until match")
    parser.add_argument("--skip-factory", action="store_true", help="Only run the light path")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    if not os.environ.get("GOOGLE_API_KEY") and not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        print("ERROR: GOOGLE_API_KEY (or GOOGLE_CLOUD_PROJECT + ADC) required", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)

    fixtures: list[dict[str, str]] = []
    if args.pdf:
        fixtures = [{"label": "custom", "path": str(args.pdf.expanduser())}]
    elif args.all_fixtures:
        fixtures = [f for f in DEFAULT_FIXTURES if Path(f["path"]).expanduser().exists()]
        if args.quick:
            fixtures = [f for f in fixtures if f["label"] in QUICK_FIXTURE_LABELS]
        missing = [f["label"] for f in DEFAULT_FIXTURES if not Path(f["path"]).expanduser().exists()]
        if missing:
            print(f"Note: skipping missing fixtures: {', '.join(missing)}")
    else:
        fixtures = [
            f
            for f in DEFAULT_FIXTURES
            if f["label"] == "bill" and Path(f["path"]).expanduser().exists()
        ]
        if not fixtures and DEFAULT_FIXTURES:
            fixtures = [
                f for f in DEFAULT_FIXTURES if Path(f["path"]).expanduser().exists()
            ][:1]

    if not fixtures:
        print("No PDF fixtures found. Pass --pdf or --all-fixtures.", file=sys.stderr)
        return 2

    results: list[dict[str, Any]] = []
    factory_cache: dict[str, dict[str, Any]] = {}
    if args.auto_climb:
        for fixture in fixtures:
            results.append(
                auto_climb_fixture(fixture, out_dir=args.out_dir, factory_cache=factory_cache)
            )
        verdict = build_verdict(results, round_num="auto-climb")
    else:
        round_num: LightRound = args.round  # type: ignore[assignment]
        for fixture in fixtures:
            results.append(
                run_one_fixture(
                    fixture,
                    round_num=round_num,
                    skip_factory=args.skip_factory,
                    out_dir=args.out_dir,
                    factory_cache=factory_cache,
                )
            )
        verdict = build_verdict(results, round_num=round_num)

    verdict_path = args.out_dir / "light_vs_factory_verdict.json"
    verdict_path.write_text(json.dumps(verdict, indent=2, default=str, ensure_ascii=False))

    summary_path = args.out_dir / "light_vs_factory_minimum.md"
    summary_path.write_text(_verdict_markdown(verdict), encoding="utf-8")

    _log("\n" + "=" * 72)
    _log(verdict["verdict_text"])
    if verdict.get("proven_minimum_round") is not None:
        minimum = verdict["proven_minimum_round"]
        rule_map = {
            1: "LLM alone (one generic read)",
            2: "LLM + tax rule (classify_invoice)",
            3: "LLM + tax + COA (categorize_invoice)",
            4: "LLM + tax + COA + route + QBS export",
        }
        _log(f"Proven minimum: Round {minimum} — {rule_map.get(minimum, '?')}")
    _log(f"-> wrote {verdict_path}")
    _log(f"-> wrote {summary_path}")
    _log("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
