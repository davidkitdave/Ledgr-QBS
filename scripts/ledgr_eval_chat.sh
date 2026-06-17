#!/usr/bin/env bash
# Chat-lane eval loop: ADK inference with session state → agents-cli grade.
#
# Usage:
#   ./scripts/ledgr_eval_chat.sh              # all B* cases
#   ./scripts/ledgr_eval_chat.sh B6_chat_...  # single case
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CASE_ID="${1:-}"
GEN_ARGS=(--lane chat --output artifacts/traces/chat_traces.json)
if [[ -n "$CASE_ID" ]]; then
  GEN_ARGS+=(--case-id "$CASE_ID")
fi

echo "==> Generate traces (ADK + session state)"
uv run python eval/ledgr_eval_generate.py "${GEN_ARGS[@]}"

echo "==> Grade traces"
agents-cli eval grade \
  --traces artifacts/traces/chat_traces.json \
  --config tests/eval/eval_config_chat.yaml \
  --output artifacts/grade_results/

echo "Done. Open artifacts/grade_results/results_*.html for details."
