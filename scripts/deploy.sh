#!/usr/bin/env bash
# Deploy Ledgr to Cloud Run (asia-southeast1) and wire SLACK_BASE_URL automatically.
#
# Usage:
#   bash scripts/deploy.sh
#
# Reads values from .env (GOOGLE_API_KEY, GOOGLE_GENAI_USE_VERTEXAI, GCS_BUCKET,
# SLACK_CLIENT_ID, SLACK_CLIENT_SECRET, SLACK_SIGNING_SECRET, SLACK_OAUTH_STATE_SECRET).
#
# Two-step (handles the redirect-URL chicken-and-egg):
#   1. deploy to capture the Cloud Run URL
#   2. set env vars (incl. SLACK_BASE_URL=<that URL>) and redeploy
# Then prints the exact URLs to paste into the Slack app config.
set -euo pipefail

REGION="${REGION:-asia-southeast1}"
PROJECT="${PROJECT:-ledgr-qbs}"
SERVICE="${SERVICE:-ledgr}"
cd "$(dirname "$0")/.."

# --- load .env (ignore comments/blank) ---
if [[ -f .env ]]; then
  set -a; # shellcheck disable=SC1091
  source .env; set +a
fi

echo "▶ Step 1/2: deploying ${SERVICE} to get the URL …"
URL="$(gcloud run deploy "${SERVICE}" --source . --region "${REGION}" \
  --allow-unauthenticated --project "${PROJECT}" \
  --format='value(status.url)')"
echo "  Cloud Run URL: ${URL}"

# --- build the env-var list from whatever is present in .env ---
ENV_PAIRS="GCS_BUCKET=${GCS_BUCKET:-ledgr-qbs-source-bucket}"
ENV_PAIRS+=",GOOGLE_GENAI_USE_VERTEXAI=${GOOGLE_GENAI_USE_VERTEXAI:-FALSE}"
[[ -n "${GOOGLE_API_KEY:-}" ]]            && ENV_PAIRS+=",GOOGLE_API_KEY=${GOOGLE_API_KEY}"
[[ -n "${SLACK_SIGNING_SECRET:-}" ]]     && ENV_PAIRS+=",SLACK_SIGNING_SECRET=${SLACK_SIGNING_SECRET}"
[[ -n "${SLACK_CLIENT_ID:-}" ]]          && ENV_PAIRS+=",SLACK_CLIENT_ID=${SLACK_CLIENT_ID}"
[[ -n "${SLACK_CLIENT_SECRET:-}" ]]      && ENV_PAIRS+=",SLACK_CLIENT_SECRET=${SLACK_CLIENT_SECRET}"
[[ -n "${SLACK_OAUTH_STATE_SECRET:-}" ]] && ENV_PAIRS+=",SLACK_OAUTH_STATE_SECRET=${SLACK_OAUTH_STATE_SECRET}"
ENV_PAIRS+=",SLACK_BASE_URL=${URL}"

echo "▶ Step 2/2: setting env vars + redeploying …"
gcloud run services update "${SERVICE}" --region "${REGION}" --project "${PROJECT}" \
  --update-env-vars "${ENV_PAIRS}"

echo ""
echo "✅ Deployed. Configure your Slack app (api.slack.com) with:"
echo "   Redirect URL (OAuth & Permissions):     ${URL}/slack/oauth_redirect"
echo "   Event Subscriptions request URL:        ${URL}/slack/events"
echo "   Interactivity request URL:              ${URL}/slack/events"
echo "   Slash command /ledgr URL:               ${URL}/slack/events"
echo "   Install link (share with each client):  ${URL}/slack/install"
echo ""
echo "   Health check: ${URL}/healthz"
if [[ -z "${SLACK_CLIENT_ID:-}" || -z "${SLACK_CLIENT_SECRET:-}" ]]; then
  echo ""
  echo "⚠ SLACK_CLIENT_ID / SLACK_CLIENT_SECRET were not in .env — OAuth install will 503"
  echo "  until you add them and re-run this script."
fi
