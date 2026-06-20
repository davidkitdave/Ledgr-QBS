"""CLI: validate a COA file locally (no Slack/Firestore).

Usage:
    python -m eval.coa_validate --file path/to/coa.xlsx
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.coa_ingest import coa_rows_from_file
from app.coa_validate import validate_coa


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a COA spreadsheet or CSV.")
    parser.add_argument("--file", required=True, help="Path to .xlsx, .xls, or .csv")
    args = parser.parse_args(argv)

    path = Path(args.file)
    if not path.is_file():
        print(f"File not found: {path}", file=sys.stderr)
        return 2

    rows = coa_rows_from_file(str(path))
    if not rows:
        print("FAIL: no accounts parsed from file")
        return 1

    result = validate_coa(rows)
    if result.ok:
        print(f"PASS: {len(rows)} accounts")
        for w in result.warnings:
            print(f"  warning: {w}")
        return 0

    print("FAIL:")
    for e in result.errors:
        print(f"  - {e}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
