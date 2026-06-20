# 0018 — CI/CD: GitHub Actions → Artifact Registry (SHA-tagged) → Cloud Run revision-tag promote

- **Status:** Accepted
- **Date:** 2026-06-18
- **Deciders:** Ledgr team

## Context

Production is currently deployed by hand via `scripts/deploy-prod.sh`
(`gcloud run deploy --source .`). There is no automated build, no test gate, no
image versioning, and no guarantee that a deploy reflects the latest green commit.
Every manual deploy is a trust exercise. We want every future deploy to the live
Slack app to provably run the **latest correct** version, with safe promotion and
instant rollback.

Two pre-conditions must be resolved before the gate goes hard:

**Ruff errors (137 at HEAD):** `ruff check` on HEAD finds 137 errors (72
auto-fixable). Shipping a hard `ruff check` gate against this tree would block the
very deploy that ships the pipeline. **Decision: adopt a curated rule set rather
than a clean-all-first approach.** The CI gate runs `ruff check --select <enforced
rules>` scoped to rules that are (a) error-free at HEAD or (b) auto-fixable by
`ruff check --fix` in the same CI step. The 72 auto-fixable errors are cleaned as
part of landing this ADR. The remaining 65 stylistic findings are documented in a
`.ruff-baseline.txt` allowlist and addressed in a dedicated follow-up. This gives
an immediately green, enforced gate without blocking the current deploy path.

**Test suite:** `uv run pytest` runs 1615 tests, 1 skipped, all hermetic (Gemini,
Slack, and Firestore are mocked at `nodes.py:250-263`). No deselection is needed.
The earlier project-memory note recommending two deselect flags is stale and
superseded by this ADR.

## Decision

### Keyless authentication — Workload Identity Federation

No JSON service-account key is stored in GitHub Secrets. CI authenticates via
**Workload Identity Federation** (WIF): a WIF pool + GitHub OIDC provider with an
`attribute-condition` pinned to this repository:

```
attribute.repository == '<owner>/Ledgr-QBS'
```

together with a ref condition scoped to `refs/heads/main`. This is security-critical:
without the `attribute-condition`, any GitHub repository could mint tokens against
the CI deploy service account.

CI deploy service account roles: `roles/artifactregistry.writer`,
`roles/run.admin`, `roles/iam.serviceAccountUser` (to act as `ledgr-runtime`),
`roles/logging.logWriter`. The Cloud Run service agent must have
`roles/artifactregistry.reader` to pull the image (typically auto-granted for
same-project images; assert it explicitly). `ledgr-runtime` must have
`datastore.user` and `aiplatform.user` (both deploy scripts assume these but do
not grant them — assert in the bootstrap script).

One-time infra is provisioned by a new `scripts/gcloud-bootstrap-cicd.sh`; the
Artifact Registry Docker repository lives at
`asia-southeast1-docker.pkg.dev/ledgr-qbs/ledgr`.

### Immutable SHA-tagged images

Every build produces an image tagged `…/ledgr/app:${GITHUB_SHA}`. Images are
never tagged `latest` in the deploy path; the SHA is the identity of what is
running. This makes rollbacks precise: "shift traffic back to the revision built
from commit `abc1234`" is an exact, auditable statement.

### Pipeline — `.github/workflows/deploy.yml` (trigger: push to `main`)

1. **Test gate** — `uv sync --frozen`; ruff per the decision above; `uv run pytest`.
   The deploy job depends on this gate passing. The existing `eval.yml` fires
   independently on `push:main` (it has no `workflow_call` entrypoint and cannot
   be invoked as a needed check as-is); `deploy.yml` runs its own full `pytest`
   regardless.
2. **Build + push** — `docker build` → tag `…/ledgr/app:${GITHUB_SHA}` → push,
   authenticated via WIF.
3. **Capture live revision** — `gcloud run services describe ledgr` captures the
   current live revision name and stores it as the rollback target **before** any
   traffic change.
4. **Deploy RC, no traffic** — `gcloud run deploy ledgr --image …:${GITHUB_SHA}
   --no-traffic --tag rc-${SHORT_SHA}`. All flags are sourced from
   `scripts/deploy-prod.sh` verbatim — it is the **single source of truth** for
   the full flag set (do not source from `scripts/gcloud-bootstrap-prod.sh`, which
   uses divergent env-var names). Required flags: `--region asia-southeast1`,
   `--project ledgr-qbs`, `--service-account ledgr-runtime@…`,
   `--allow-unauthenticated` (Slack must reach it), `--min-instances 1`,
   `--max-instances 1`, `--set-secrets` for `SLACK_CLIENT_SECRET` and
   `SLACK_SIGNING_SECRET`, and `--set-env-vars` for `LEDGR_ENV=prod`,
   `SLACK_BASE_URL`, `SLACK_CLIENT_ID`, `GOOGLE_CLOUD_PROJECT`,
   `GOOGLE_CLOUD_LOCATION`, `FIRESTORE_PROJECT`,
   `GOOGLE_GENAI_USE_VERTEXAI=TRUE`, `LEDGR_MODEL_LITE`, `LEDGR_MODEL_STD`.
   A CI parity assertion checks that the flag set matches `deploy-prod.sh` at
   build time.
5. **Smoke test the RC** — hit the RC's tagged URL. `/healthz`
   (`accounting_agents/fast_api_app.py:69`) is a static liveness probe; a new
   `/readyz` endpoint asserts that the Bolt handler and app lifespan have
   initialised, providing a meaningful smoke gate. No real Slack traffic is sent
   to the RC revision.
6. **Manual promote** — a GitHub **Environment `production`** with required
   reviewers gates the traffic shift. A human approves before any traffic moves.
7. **Traffic shift** — `gcloud run services update-traffic ledgr
   --to-tags rc-${SHORT_SHA}=100`.
8. **Rollback job** (`workflow_dispatch`) — `gcloud run services update-traffic
   ledgr --to-revisions <captured-prior-revision>=100`. Rollback targets the
   specific named revision captured in step 3, not a tag.

### "Latest correct version" guarantee

Immutable SHA images + green-main test gate + human promote gate + no-traffic RC
smoke test + traffic-shift rollback to a named prior revision. Every deployed
revision is traceable to a specific commit, a passing test run, and a named
approver.

## Trade-offs

- **Manual promote vs auto-deploy.** Auto-deploy to production on every main merge
  would be faster but removes the human check before Slack customers are affected.
  Given that the Slack app runs in customer workspaces (Model B), a bad deploy
  breaks live firm channels instantly. Manual promote with a no-traffic RC keeps
  the iteration speed high while preserving a safety checkpoint.
- **Curated ruff gate vs clean-all-first.** Cleaning all 137 errors before shipping
  the gate adds scope to a pipeline ADR and risks introducing regressions in a
  large stylistic sweep. The curated-rule approach ships an enforceable gate now
  and tracks the remainder as a known backlog item.
- **No staging service.** A separate staging Cloud Run service and staging Slack
  app would give a closer production mirror. The no-traffic RC revision on the
  production service achieves comparable isolation without doubling the infra
  footprint. The smoke test hits the RC URL directly; Slack traffic cannot reach
  it without an explicit traffic shift.
- **Single source of truth for flags.** Sourcing the full flag set from
  `deploy-prod.sh` rather than from the bootstrap script prevents the flag
  divergence (different env-var names for `VERTEX_PROJECT_ID` vs
  `GOOGLE_CLOUD_PROJECT`) observed at HEAD.

## Alternatives considered

- **JSON service-account key in GitHub Secrets** — simpler setup but a long-lived
  credential that cannot be scoped to a single repository. Rejected in favour of
  keyless WIF.
- **`gcloud run deploy --source .` in CI** — builds the image inside Cloud Run's
  build pipeline, losing the SHA-tagged immutable image. Rollback becomes
  "redeploy from the commit", not "shift traffic to the prior revision". Rejected.
- **Automatic traffic shift on green test** — no human gate. Rejected for a
  customer-facing Slack app with no staging service.
- **Clean all 137 ruff errors first** — correct eventual state, but out of scope
  for a CI/CD ADR and risks blocking the pipeline on a style sweep. Deferred to
  a follow-up.
