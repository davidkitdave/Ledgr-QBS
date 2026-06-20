#!/usr/bin/env bash
# scripts/gcloud-bootstrap-cicd.sh
#
# One-time CI/CD infrastructure bootstrap for Ledgr-QBS.
# Run this ONCE (as a human with Owner/Editor + IAM Admin) before the first
# GitHub Actions deploy. It is idempotent where the gcloud CLI supports it.
#
# What it provisions:
#   1. Artifact Registry Docker repo (asia-southeast1-docker.pkg.dev/ledgr-qbs/ledgr).
#   2. Workload Identity Federation pool + GitHub OIDC provider (keyless CI auth).
#   3. CI deploy service account (ledgr-cicd) with the roles needed to push
#      images and deploy Cloud Run revisions.
#   4. WIF principal-set → CI SA binding (workloadIdentityUser).
#   5. Assertion that the Cloud Run service agent can pull images from AR.
#   6. Assertion that ledgr-runtime has roles/datastore.user + roles/aiplatform.user.
#
# Usage:
#   1. Fill in GH_OWNER below (your GitHub org or username, e.g. "acme-corp").
#   2. gcloud auth login --update-adc          # human, with sufficient IAM rights
#   3. bash scripts/gcloud-bootstrap-cicd.sh
#
# At the end the script prints the two values you must paste into GitHub:
#   - workload_identity_provider
#   - service_account (the CI deploy SA email)
#
# These go into a GitHub Environment named "production" (or repo-level vars/secrets)
# as WORKLOAD_IDENTITY_PROVIDER and CICD_SERVICE_ACCOUNT.

set -euo pipefail

# ---------------------------------------------------------------------------
# FILL IN BEFORE RUNNING
# ---------------------------------------------------------------------------
# Your GitHub owner (org or personal account name) — e.g. "acme-corp" or "jsmith".
# This is the part before "/Ledgr-QBS" in your repo URL.
GH_OWNER="${GH_OWNER:-<OWNER>}"

# ---------------------------------------------------------------------------
# Fixed project config — matches deploy-prod.sh exactly.
# ---------------------------------------------------------------------------
PROJECT="${PROJECT:-ledgr-qbs}"
REGION="${REGION:-asia-southeast1}"
GH_REPO="${GH_OWNER}/Ledgr-QBS"

# CI/CD-specific identifiers.
WIF_POOL_ID="github-actions-pool"
WIF_PROVIDER_ID="github-oidc"
CICD_SA_NAME="ledgr-cicd"
CICD_SA_EMAIL="${CICD_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

# Runtime SA — must match deploy-prod.sh exactly.
RUNTIME_SA_EMAIL="ledgr-runtime@${PROJECT}.iam.gserviceaccount.com"

# Artifact Registry repo.
AR_REPO="ledgr"
AR_LOCATION="${REGION}"
AR_HOST="${AR_LOCATION}-docker.pkg.dev"

# ---------------------------------------------------------------------------
# Colour helpers (match gcloud-bootstrap-prod.sh style).
# ---------------------------------------------------------------------------
err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
note() { printf '\033[36m%s\033[0m\n' "$*" >&2; }
ok()   { printf '\033[32m%s\033[0m\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# 0. Guard: require GH_OWNER to be set.
# ---------------------------------------------------------------------------
if [[ "$GH_OWNER" == "<OWNER>" ]]; then
  err "Set GH_OWNER at the top of this script (or export GH_OWNER=<your-github-org>) before running."
  exit 1
fi

note "Project : $PROJECT"
note "Region  : $REGION"
note "GH repo : $GH_REPO"

# ---------------------------------------------------------------------------
# 1. Enable APIs.
# ---------------------------------------------------------------------------
note "Enabling required APIs (idempotent)..."
gcloud services enable \
  artifactregistry.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  cloudresourcemanager.googleapis.com \
  run.googleapis.com \
  --project "$PROJECT" \
  --quiet
ok "APIs enabled."

# ---------------------------------------------------------------------------
# 2. Artifact Registry Docker repo.
# ---------------------------------------------------------------------------
note "Creating Artifact Registry Docker repo '${AR_REPO}' in ${AR_LOCATION}..."
if gcloud artifacts repositories describe "$AR_REPO" \
     --location="$AR_LOCATION" --project="$PROJECT" --quiet >/dev/null 2>&1; then
  note "  Repo already exists — skipping create."
else
  gcloud artifacts repositories create "$AR_REPO" \
    --repository-format=docker \
    --location="$AR_LOCATION" \
    --project="$PROJECT" \
    --description="Ledgr app images (SHA-tagged, CI-built)" \
    --quiet
  ok "  Artifact Registry repo created: ${AR_HOST}/${PROJECT}/${AR_REPO}"
fi

# ---------------------------------------------------------------------------
# 3. Workload Identity Federation pool.
# ---------------------------------------------------------------------------
note "Creating WIF pool '${WIF_POOL_ID}'..."
if gcloud iam workload-identity-pools describe "$WIF_POOL_ID" \
     --location=global --project="$PROJECT" --quiet >/dev/null 2>&1; then
  note "  WIF pool already exists — skipping create."
else
  gcloud iam workload-identity-pools create "$WIF_POOL_ID" \
    --location=global \
    --project="$PROJECT" \
    --display-name="GitHub Actions pool" \
    --quiet
  ok "  WIF pool created."
fi

# ---------------------------------------------------------------------------
# 4. GitHub OIDC provider — with attribute-condition pinned to this repo.
#
#    WHY THE attribute-condition IS SECURITY-CRITICAL:
#    Without it, ANY GitHub repository could obtain tokens for the CI deploy
#    service account (the OIDC issuer is shared across all of github.com).
#    The condition below restricts token issuance to:
#      - Exactly this repository (attribute.repository == GH_REPO), AND
#      - Only the main branch (attribute.ref == "refs/heads/main").
#    This means a fork, a PR branch, or any other repo cannot impersonate the
#    CI deploy SA and push images or deploy revisions.
# ---------------------------------------------------------------------------
note "Creating GitHub OIDC provider '${WIF_PROVIDER_ID}'..."
WIF_ATTRIBUTE_CONDITION="attribute.repository == \"${GH_REPO}\" && attribute.ref == \"refs/heads/main\""

if gcloud iam workload-identity-pools providers describe "$WIF_PROVIDER_ID" \
     --workload-identity-pool="$WIF_POOL_ID" \
     --location=global --project="$PROJECT" --quiet >/dev/null 2>&1; then
  note "  OIDC provider already exists — skipping create."
  note "  (Re-run with --update if you need to change the attribute-condition.)"
else
  gcloud iam workload-identity-pools providers create-oidc "$WIF_PROVIDER_ID" \
    --workload-identity-pool="$WIF_POOL_ID" \
    --location=global \
    --project="$PROJECT" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition="${WIF_ATTRIBUTE_CONDITION}" \
    --display-name="GitHub OIDC Ledgr-QBS" \
    --quiet
  ok "  OIDC provider created with attribute-condition: ${WIF_ATTRIBUTE_CONDITION}"
fi

# Resolve the full WIF provider resource name (needed for GitHub secret value).
WIF_PROVIDER_RESOURCE="$(gcloud iam workload-identity-pools providers describe "$WIF_PROVIDER_ID" \
  --workload-identity-pool="$WIF_POOL_ID" \
  --location=global \
  --project="$PROJECT" \
  --format='value(name)')"

# ---------------------------------------------------------------------------
# 5. CI deploy service account.
# ---------------------------------------------------------------------------
note "Creating CI deploy service account '${CICD_SA_EMAIL}'..."
if gcloud iam service-accounts describe "$CICD_SA_EMAIL" \
     --project="$PROJECT" --quiet >/dev/null 2>&1; then
  note "  Service account already exists — skipping create."
else
  gcloud iam service-accounts create "$CICD_SA_NAME" \
    --project="$PROJECT" \
    --display-name="Ledgr CI/CD deploy (GitHub Actions)" \
    --quiet
  ok "  CI SA created: ${CICD_SA_EMAIL}"
fi

# ---------------------------------------------------------------------------
# 6. Grant CI SA the roles it needs.
#    - artifactregistry.writer  : push SHA-tagged images.
#    - run.admin                : deploy Cloud Run revisions + update traffic.
#    - iam.serviceAccountUser   : act as ledgr-runtime when deploying.
#    - logging.logWriter        : write build/deploy logs from CI.
# ---------------------------------------------------------------------------
note "Granting roles to CI SA (idempotent add-iam-policy-binding)..."
for role in \
  roles/artifactregistry.writer \
  roles/run.admin \
  roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${CICD_SA_EMAIL}" \
    --role="$role" \
    --condition=None \
    --quiet >/dev/null
  ok "  Granted ${role} to ${CICD_SA_EMAIL}"
done

# serviceAccountUser must be granted on the runtime SA resource, not the project,
# to follow least-privilege (CI can only impersonate ledgr-runtime, not any SA).
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA_EMAIL" \
  --project="$PROJECT" \
  --member="serviceAccount:${CICD_SA_EMAIL}" \
  --role="roles/iam.serviceAccountUser" \
  --quiet >/dev/null
ok "  Granted roles/iam.serviceAccountUser on ${RUNTIME_SA_EMAIL} to ${CICD_SA_EMAIL}"

# ---------------------------------------------------------------------------
# 7. Bind WIF principalSet to CI SA (workloadIdentityUser).
#    This is what allows the GitHub Actions OIDC token to impersonate the CI SA.
# ---------------------------------------------------------------------------
note "Binding WIF principal-set to CI SA..."
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
WIF_PRINCIPAL_SET="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL_ID}/attribute.repository/${GH_REPO}"

gcloud iam service-accounts add-iam-policy-binding "$CICD_SA_EMAIL" \
  --project="$PROJECT" \
  --member="${WIF_PRINCIPAL_SET}" \
  --role="roles/iam.workloadIdentityUser" \
  --quiet >/dev/null
ok "  WIF binding set: ${WIF_PRINCIPAL_SET} -> ${CICD_SA_EMAIL}"

# ---------------------------------------------------------------------------
# 8. Assert Cloud Run service agent can pull from Artifact Registry.
#    Cloud Run uses a per-project service agent: service-<PROJECT_NUMBER>@serverless-robot-prod.iam.gserviceaccount.com
#    For same-project AR repos this is typically auto-granted, but we assert it
#    explicitly per ADR-0018 to avoid silent pull failures.
# ---------------------------------------------------------------------------
note "Asserting Cloud Run service agent has roles/artifactregistry.reader..."
CR_SERVICE_AGENT="service-${PROJECT_NUMBER}@serverless-robot-prod.iam.gserviceaccount.com"
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" \
  --location="$AR_LOCATION" \
  --project="$PROJECT" \
  --member="serviceAccount:${CR_SERVICE_AGENT}" \
  --role="roles/artifactregistry.reader" \
  --quiet >/dev/null
ok "  Cloud Run service agent granted roles/artifactregistry.reader on ${AR_REPO}."

# ---------------------------------------------------------------------------
# 9. Assert ledgr-runtime has datastore.user + aiplatform.user.
#    Both deploy scripts assume these roles exist. This script makes it explicit.
# ---------------------------------------------------------------------------
note "Asserting ledgr-runtime has datastore.user + aiplatform.user..."
for role in roles/datastore.user roles/aiplatform.user; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
    --role="$role" \
    --condition=None \
    --quiet >/dev/null
  ok "  ${RUNTIME_SA_EMAIL} has ${role}"
done

# ---------------------------------------------------------------------------
# Summary — values the user must paste into GitHub.
# ---------------------------------------------------------------------------
cat <<EOF >&2

============================================================
Bootstrap complete. One-time steps above are done.

STEP 1 — Repository variables (Settings → Secrets and variables → Actions → Variables tab):
  These are REPOSITORY-level variables, NOT Environment-scoped.
  Every workflow job resolves them via vars.* without an \`environment:\` declaration.
  None are sensitive values.

  Variable name               Value
  ─────────────────────────── ──────────────────────────────────────────────────────
  WORKLOAD_IDENTITY_PROVIDER  ${WIF_PROVIDER_RESOURCE}
  CICD_SERVICE_ACCOUNT        ${CICD_SA_EMAIL}
  SLACK_BASE_URL              https://ledgr-640071771526.asia-southeast1.run.app
  SLACK_CLIENT_ID             11179968143121.11331108897447
  LEDGR_MODEL_LITE            gemini-2.5-flash
  LEDGR_MODEL_STD             gemini-2.5-flash

STEP 2 — GitHub Environment "production" (Settings → Environments → New environment):
  - Name it exactly: production
  - Add required reviewers (the human approval gate for traffic-shift and rollback jobs).
  - Store NO variables or secrets in this Environment — it exists solely as the
    approval gate. All variables are at repository level (Step 1 above).

STEP 3 — No GitHub secrets needed:
  SLACK_CLIENT_SECRET and SLACK_SIGNING_SECRET are already in GCP Secret Manager.
  Cloud Run pulls them at deploy time via --set-secrets. Do NOT store them in GitHub.

WIF provider resource name (for reference):
  ${WIF_PROVIDER_RESOURCE}
============================================================
EOF
