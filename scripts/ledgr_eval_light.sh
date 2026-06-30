#!/usr/bin/env bash
# Quality Flywheel entrypoint for ledgr_agent reference-free financial-doc eval.
#
# Tier 2 (this script): agents-cli eval generate + grade with ALL metrics in
#   ledgr_agent/eval/eval_config_ledgr_light.yaml (7 metrics: built-in + extraction_*).
#   Regenerate fixtures first: uv run python -m ledgr_agent.eval.build_cases
#
# Optional GEPA instruction polish (schema descriptions are manual):
#   uv run python -m ledgr_agent.eval.optimize.run_optimize
#
# Tier 3 (CI / full ADK gate): same metrics via pytest AgentEvaluator:
#   LEDGR_EVAL_DUMP=artifacts/grade_results/ledgr_light_run.json \
#     uv run pytest ledgr_agent/eval/test_h_ledgr_light_live.py -m eval
#
# Usage:
#   ./scripts/ledgr_eval_light.sh
#   ./scripts/ledgr_eval_light.sh compare artifacts/grade_results/baseline.json artifacts/grade_results/new.json
#
# Requires GOOGLE_API_KEY (or Vertex ADC). Skips gracefully when creds missing.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DATASET="ledgr_agent/eval/datasets/ledgr_light_cases.json"
CONFIG="ledgr_agent/eval/eval_config_ledgr_light.yaml"
BASELINE="artifacts/grade_results/ledgr_light_baseline.json"
OUT_DIR="artifacts/grade_results"
TRACES="artifacts/traces/ledgr_light_traces.json"

mkdir -p "$(dirname "$TRACES")" "$OUT_DIR"

if [[ "${1:-}" == "compare" ]]; then
  BASE="${2:?usage: $0 compare <baseline.json> <new.json>}"
  NEW="${3:?usage: $0 compare <baseline.json> <new.json>}"
  uv run python scripts/ledgr_eval_compare.py "$BASE" "$NEW"
  exit 0
fi

if [[ -z "${GOOGLE_API_KEY:-}" && -z "${GOOGLE_CLOUD_PROJECT:-}" ]]; then
  echo "Skip: set GOOGLE_API_KEY or GOOGLE_CLOUD_PROJECT for live eval." >&2
  exit 0
fi

agents-cli eval generate \
  --dataset "$DATASET" \
  --output "$TRACES"

RESULTS="$OUT_DIR/results_$(date +%Y%m%d_%H%M%S).json"
agents-cli eval grade \
  --traces "$TRACES" \
  --config "$CONFIG" \
  --output "$RESULTS"

echo "Wrote $RESULTS (full metric set — see script header for pytest parity gate)"
echo "Compare: $0 compare $BASELINE $RESULTS"
