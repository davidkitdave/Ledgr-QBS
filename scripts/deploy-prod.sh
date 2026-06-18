#!/usr/bin/env bash
#
# Canonical PRODUCTION deploy of the Ledgr Slack app to Cloud Run.
# This is the single source of truth for the prod deploy command — keep it in sync
# with what actually runs. Manual source-build deploy (no CI/CD trigger yet); builds
# from the local working tree via the repo Dockerfile.
#
# Prerequisites (one-time, see scripts/gcloud-bootstrap-prod.sh):
#   - Secrets in Secret Manager: SLACK_CLIENT_SECRET, SLACK_SIGNING_SECRET
#   - Runtime SA ledgr-runtime@<project> granted: roles/datastore.user,
#     roles/aiplatform.user, and secretmanager.secretAccessor on the two secrets
#
# Usage:  bash scripts/deploy-prod.sh
set -euo pipefail

PROJECT="${PROJECT:-ledgr-qbs}"
REGION="${REGION:-asia-southeast1}"
SERVICE="${SERVICE:-ledgr}"
SA="ledgr-runtime@${PROJECT}.iam.gserviceaccount.com"

# Deterministic Cloud Run URL (<service>-<project-number>.<region>.run.app). MUST match
# the Slack app's OAuth Redirect URL and the Event/Interactivity/Slash-command Request URLs.
BASE_URL="${BASE_URL:-https://ledgr-640071771526.asia-southeast1.run.app}"

# Slack Client ID is public (it appears in the install URL) — not a secret.
SLACK_CLIENT_ID="${SLACK_CLIENT_ID:-11179968143121.11331108897447}"

# Model tiers. NOTE: gemini-2.5-flash-lite is NOT served in any Asia Vertex region
# (404 in asia-southeast1). We keep Vertex in asia-southeast1 for PDPA / Singapore data
# residency, so the LITE tier uses gemini-2.5-flash here. To use flash-lite you must
# move GOOGLE_CLOUD_LOCATION to global/us-central1 (data then leaves Singapore).
LEDGR_MODEL_LITE="${LEDGR_MODEL_LITE:-gemini-2.5-flash}"
LEDGR_MODEL_STD="${LEDGR_MODEL_STD:-gemini-2.5-flash}"

# min=max=1: single instance avoids the in-memory event-dedup gap (a multi-instance
# deployment must back dedup with Firestore first — see app/slack_app.py _SeenEvents).
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --project "$PROJECT" \
  --service-account "$SA" \
  --set-secrets "SLACK_CLIENT_SECRET=SLACK_CLIENT_SECRET:latest,SLACK_SIGNING_SECRET=SLACK_SIGNING_SECRET:latest" \
  --set-env-vars "LEDGR_ENV=prod,SLACK_CLIENT_ID=${SLACK_CLIENT_ID},SLACK_BASE_URL=${BASE_URL},GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=${REGION},FIRESTORE_PROJECT=${PROJECT},LEDGR_MODEL_LITE=${LEDGR_MODEL_LITE},LEDGR_MODEL_STD=${LEDGR_MODEL_STD}" \
  --min-instances 1 \
  --max-instances 1 \
  --allow-unauthenticated
