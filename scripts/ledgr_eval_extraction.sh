#!/usr/bin/env bash
# G-cluster extraction golden eval (WS-0.2) — document lane, ADR-0023.
#
# Uses evalset cases + custom metrics (eval_config_extraction.yaml).
# NOT agents-cli generate — PDF extraction is pytest-driven.
#
# Requires local PDFs under ~/Desktop/LocalTest/TestDoc/MYDoc/ and
# GOOGLE_API_KEY or GOOGLE_CLOUD_PROJECT for live extraction cases.
#
# Usage:
#   ./scripts/ledgr_eval_extraction.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> G-cluster extraction golden (pytest + custom metrics)"
uv run pytest tests/eval/test_g_extraction_golden.py -m eval -v

echo "Done. Metrics defined in tests/eval/eval_config_extraction.yaml"
