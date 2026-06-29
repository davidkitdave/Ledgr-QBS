# Deployment Guide

Production runs on **Google Cloud Run** (`asia-southeast1`), serving the FastAPI app
built by `accounting_agents.slack_runner.build_fastapi_app` behind `app.main:app`.

**CI/CD decision record:** [`docs/adr/0018-cicd-github-actions-artifact-registry-cloud-run.md`](../docs/adr/0018-cicd-github-actions-artifact-registry-cloud-run.md)

---

## Runtime paths (document processing)

Slack file drops follow one of two paths (controlled by `LEDGR_USE_CLEAN_AGENT`):

| Path | When | Entry | HITL |
|------|------|-------|------|
| **Legacy graph** (default) | `LEDGR_USE_CLEAN_AGENT` unset / `0` | `accounting_agents` ADK `Workflow` via `process_file_event` | ADK `RequestInput` nodes |
| **Clean agent** (cutover target) | `LEDGR_USE_CLEAN_AGENT=1` | `accounting_agents/clean_agent_slack.py` → `process_document_batch` tool | Firestore interrupt (`CLEAN_AGENT_HITL_KIND`) |

Both paths share the same Slack I/O, credit gate, and delivery layer in
`slack_runner.py`. On clean-agent failure the runner **falls through** to the
legacy graph. See ADR-0026 and
[`docs/qa/clean-agent-dev-qa-runbook.md`](../docs/qa/clean-agent-dev-qa-runbook.md).

**Cutover note:** `scripts/deploy-prod.sh` does **not** set `LEDGR_USE_CLEAN_AGENT`
today — prod still runs the legacy graph until an operator adds the flag to the
Cloud Run revision env (Plan D.6).

**Credits on Cloud Run:** `build_runner()` calls `wire_shared_credit_service()`,
which installs `FirestoreCreditStore` when `K_SERVICE` is set (ADR-0016). Dev
socket mode uses `InMemoryCreditStore` unless you set
`GOOGLE_APPLICATION_CREDENTIALS`; grant test credits with `LEDGR_DEV_CREDIT_GRANTS`.

---

## Automated deploy (normal path)

Every merge to `main` triggers `.github/workflows/deploy.yml`:

1. **Test gate** — `uv sync --frozen`, `ruff check .`, `uv run pytest` (all hermetic).
2. **Build + push** — Docker image tagged `asia-southeast1-docker.pkg.dev/ledgr-qbs/ledgr/app:${GITHUB_SHA}`.
3. **Deploy** — `gcloud run deploy ledgr` with the full flag set from
   `scripts/deploy-prod.sh` (single source of truth for env vars, secrets, scaling).
   The new revision receives **100% traffic immediately** — there is no
   no-traffic RC or manual promote gate (simplified 2026-06-20; see ADR-0018 update).

### Prerequisites (one-time)

Run `scripts/gcloud-bootstrap-cicd.sh` to provision:

- Workload Identity Federation (keyless GitHub → GCP auth)
- Artifact Registry repository `ledgr-qbs/ledgr`
- CI deploy service account with `run.admin`, `artifactregistry.writer`, etc.

Set these **GitHub repository variables** (Actions → Variables):

| Variable | Purpose |
|----------|---------|
| `WORKLOAD_IDENTITY_PROVIDER` | WIF provider resource name |
| `CICD_SERVICE_ACCOUNT` | e.g. `ledgr-cicd@ledgr-qbs.iam.gserviceaccount.com` |
| `SLACK_BASE_URL` | Cloud Run service URL (must match Slack app manifest) |
| `SLACK_CLIENT_ID` | Public OAuth client ID |
| `LEDGR_MODEL_LITE` / `LEDGR_MODEL_STD` | Vertex model tiers |

`SLACK_CLIENT_SECRET` and `SLACK_SIGNING_SECRET` live in **GCP Secret Manager**;
Cloud Run pulls them via `--set-secrets` at deploy time — no GitHub secrets needed.

### Rollback

In GitHub Actions, run **Deploy to Cloud Run** with `workflow_dispatch` and set
`rollback_revision` to a prior named revision (e.g. `ledgr-00042-abc`). This shifts
100% traffic back without rebuilding.

---

## Manual deploy (emergency / bootstrap)

```bash
bash scripts/deploy-prod.sh
```

Builds from the **local working tree** via `gcloud run deploy --source .`. Use only
when CI is unavailable. Keep `scripts/deploy-prod.sh` in sync with the workflow's
flag set — the workflow asserts parity at build time.

### Runtime configuration (prod)

| Setting | Value |
|---------|-------|
| Service account | `ledgr-runtime@ledgr-qbs.iam.gserviceaccount.com` |
| `LEDGR_ENV` | `prod` |
| `LEDGR_USE_CLEAN_AGENT` | **unset** (legacy graph) — set `1` when cutover approved |
| Gemini | Vertex AI (`GOOGLE_GENAI_USE_VERTEXAI=TRUE`) in `asia-southeast1` |
| Firestore | `FIRESTORE_PROJECT=ledgr-qbs`, **no** `LEDGR_FIRESTORE_NAMESPACE` |
| Scaling | `--min-instances 1 --max-instances 1` (in-memory dedup; see ADR) |
| Auth | `--allow-unauthenticated` (Slack must reach `/slack/events`) |

Model note: `gemini-2.5-flash-lite` is not served in Asia Vertex regions; prod uses
`gemini-2.5-flash` for both LITE and STD tiers to keep data in Singapore (PDPA).

---

## Verify a deployment

```bash
# Process up (no Slack creds needed locally)
curl -sS http://localhost:8080/openapi.json | head

# Readiness (needs Slack HTTP + OAuth env vars in the container)
curl -sS -o /dev/null -w "%{http_code}\n" https://<service-url>/healthz
# 200 = config present; 503 + {"missing": [...]} = expected until secrets mount
```

Slack app manifest URLs (`slack/manifest-qbs.json`) must match `SLACK_BASE_URL`:
Event Subscriptions, Interactivity, OAuth redirect, slash commands.

---

## Slack app setup

See [`slack/README.md`](../slack/README.md) for manifest paste + reinstall steps.

---

## Not this guide

- **Agent Runtime / `adk deploy agent_engine`** — not used for Ledgr production.
- **`agents-cli deploy`** — dev playground tooling only; prod is Cloud Run + WIF.
- **Local dev** — socket mode via `slack_bot.py`; see [`docs/dev-environment.md`](../docs/dev-environment.md).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Deploy workflow fails at ruff/pytest | Bad commit on main | Fix tests locally, push |
| `/healthz` 503 in prod | Secret Manager mount or env var gap | Check Cloud Run revision env + secrets |
| Slack events 500 | Missing Firestore ADC or bad signing secret | Cloud Run logs; verify `FIRESTORE_PROJECT` |
| Deploy OK but Slack unchanged | `SLACK_BASE_URL` / manifest URL mismatch | Align manifest request URLs with service URL |
| Wrong Firestore data | Dev namespace leaked to prod | Prod must **not** set `LEDGR_FIRESTORE_NAMESPACE` (ADR-0022) |
| Re-import shows "Already recorded" after month clear | `seen_doc_keys` not purged for cleared month | See month-clear notes in [`docs/dev-environment.md`](../docs/dev-environment.md) |

Logs:

```bash
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=ledgr" \
  --project=ledgr-qbs --limit=20 --format="table(timestamp,textPayload)"
```
