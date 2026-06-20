#!/usr/bin/env bash
# One-shot bootstrap for Ledgr prod secrets on Google Cloud.
#
# What it does:
#   1. Confirms gcloud auth + project + region.
#   2. Enables Secret Manager + Cloud Run APIs (idempotent).
#   3. Prompts for each Slack secret value via silent stdin and stores it in Secret Manager.
#   4. Grants the Cloud Run runtime service account read access on each secret.
#   5. Prints the gcloud run deploy command pre-filled with --set-secrets / --set-env-vars
#      that you can copy-paste for the actual deploy.
#
# Safe to re-run: secrets are added as new versions; IAM bindings are idempotent.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-ledgr-qbs}"
REGION="${REGION:-asia-southeast1}"
SERVICE_NAME="${SERVICE_NAME:-ledgr}"

SECRETS=(SLACK_CLIENT_SECRET SLACK_SIGNING_SECRET)

err()  { printf '\033[31m%s\033[0m\n' "$*" >&2 ; }
note() { printf '\033[36m%s\033[0m\n' "$*" >&2 ; }
ok()   { printf '\033[32m%s\033[0m\n' "$*" >&2 ; }

# --- 0. Auth + project sanity ---
ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null || true)"
[[ -z "$ACCOUNT" ]] && { err "No active gcloud account. Run: gcloud auth login"; exit 1; }
note "gcloud account: $ACCOUNT"

gcloud config set project "$PROJECT_ID" --quiet >/dev/null
note "project: $PROJECT_ID | region: $REGION | service: $SERVICE_NAME"

# --- 1. Enable APIs ---
note "Enabling Secret Manager + Cloud Run APIs (idempotent)..."
gcloud services enable secretmanager.googleapis.com run.googleapis.com iam.googleapis.com --quiet

# --- 2. Resolve / create runtime service account ---
SA_EMAIL="$(gcloud run services describe "$SERVICE_NAME" --region="$REGION" \
            --format='value(spec.template.spec.serviceAccountName)' 2>/dev/null || true)"

if [[ -z "$SA_EMAIL" ]]; then
  SA_NAME="ledgr-runtime"
  SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
  if ! gcloud iam service-accounts describe "$SA_EMAIL" --quiet >/dev/null 2>&1 ; then
    note "Creating runtime service account: $SA_EMAIL"
    gcloud iam service-accounts create "$SA_NAME" \
      --display-name="Ledgr Cloud Run runtime" --quiet
  else
    note "Using existing service account: $SA_EMAIL"
  fi
else
  note "Using deployed service's account: $SA_EMAIL"
fi

# --- 3. Prompt for + write secrets ---
for name in "${SECRETS[@]}" ; do
  if gcloud secrets describe "$name" --quiet >/dev/null 2>&1 ; then
    note "Secret '$name' already exists — adding a new version."
  else
    note "Creating secret '$name'."
    gcloud secrets create "$name" --replication-policy=automatic --quiet
  fi

  printf '\nPaste value for %s (input hidden, ENTER when done): ' "$name" >&2
  IFS= read -rs value
  printf '\n' >&2
  [[ -z "$value" ]] && { err "  empty value — skipping $name"; continue; }

  printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- --quiet >/dev/null
  unset value
  ok "  stored a new version of $name"

  gcloud secrets add-iam-policy-binding "$name" \
    --member="serviceAccount:$SA_EMAIL" \
    --role="roles/secretmanager.secretAccessor" \
    --quiet >/dev/null
  ok "  granted $SA_EMAIL read access on $name"
done

# --- 4. Emit the ready-to-run deploy command ---
cat <<EOF >&2

------------------------------------------------------------
Bootstrap complete. To deploy Cloud Run, run:

gcloud run deploy $SERVICE_NAME \\
  --region=$REGION \\
  --source=. \\
  --service-account=$SA_EMAIL \\
  --set-env-vars="LEDGR_ENV=prod,SLACK_CLIENT_ID=<paste your Slack client id>,GOOGLE_GENAI_USE_VERTEXAI=TRUE,VERTEX_PROJECT_ID=$PROJECT_ID,VERTEX_LOCATION=$REGION,FIRESTORE_PROJECT=$PROJECT_ID" \\
  --set-secrets="SLACK_CLIENT_SECRET=SLACK_CLIENT_SECRET:latest,SLACK_SIGNING_SECRET=SLACK_SIGNING_SECRET:latest" \\
  --allow-unauthenticated

(SLACK_CLIENT_ID is the app's public Client ID from api.slack.com — fine to
paste into --set-env-vars; only the *Secret* values went through Secret
Manager above.)
------------------------------------------------------------
EOF
