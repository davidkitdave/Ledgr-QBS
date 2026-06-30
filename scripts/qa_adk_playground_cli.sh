#!/usr/bin/env bash
# Control-CLI harness: credit balance + optional search flag check (ADK playground QA).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a && source .env && set +a

export LEDGR_DEV_CREDIT_GRANTS="${LEDGR_DEV_CREDIT_GRANTS:-T_PLAYGROUND:50}"
export LEDGR_PLAYGROUND_PROFILE_PATH="${LEDGR_PLAYGROUND_PROFILE_PATH:-playground_profile.json}"

echo "=== CLI: read_credit_balance (dev grant ${LEDGR_DEV_CREDIT_GRANTS}) ==="
uv run python - <<'PY'
from types import SimpleNamespace

from ledgr_slack.credit_adapter import wire_shared_credit_service
from ledgr_agent.billing import read_credit_balance

wire_shared_credit_service()
result = read_credit_balance(
    SimpleNamespace(state={"firm_id": "T_PLAYGROUND", "client_id": "playground"})
)
print(result)
assert result["status"] == "success", result
assert result["balance"] == 50, result
print("CLI credit check: PASS")
PY

echo ""
echo "=== CLI: zero-balance gate (billing.gate) ==="
uv run python - <<'PY'
from types import SimpleNamespace

from ledgr_agent.billing import CreditService, InMemoryCreditStore, configure_shared_credit_service, gate

configure_shared_credit_service(CreditService(InMemoryCreditStore()))
blocked = gate(SimpleNamespace(state={"firm_id": "T_EMPTY"}), units=1, kind="bill")
print("credit_status:", blocked.credit_status if blocked else None)
assert blocked is not None
assert blocked.credit_status == "blocked"
print("CLI gate check: PASS")
PY

echo ""
echo "Done. Start ADK web with:"
echo "  export LEDGR_DEV_CREDIT_GRANTS=${LEDGR_DEV_CREDIT_GRANTS}"
echo "  export LEDGR_ENABLE_WEB_SEARCH=1   # optional search sub-agent"
echo "  uv run adk web ledgr_agent --port 8090 --host 127.0.0.1"
