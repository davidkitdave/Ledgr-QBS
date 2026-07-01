# Slack App Setup Guide

This guide takes you from zero to a running Ledgr Slack bot.  
The only things you need to supply are tokens — everything else is already wired.

---

## Required environment variables

| Variable | Where to get it | Required for |
|---|---|---|
| `SLACK_BOT_TOKEN` | OAuth & Permissions → Bot User OAuth Token (`xoxb-…`) | Both modes |
| `SLACK_SIGNING_SECRET` | Basic Information → App Credentials → Signing Secret | HTTP (Cloud Run) mode |
| `SLACK_APP_TOKEN` | Basic Information → App-Level Tokens (`xapp-…`) | Socket Mode (local testing) |
| `PROJECT_ID` / `GOOGLE_CLOUD_PROJECT` | Your GCP project ID | Firestore / Vertex AI |
| `LOCATION` | GCP region, e.g. `asia-southeast1` | Vertex AI |

---

## Step 1 — Create the Slack app from the manifest

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App**.
2. Choose **From an app manifest**.
3. Select your workspace and click **Next**.
4. Paste the contents of [`slack/manifest.json`](../slack/manifest.json) into the JSON editor and click **Next** → **Create**.

The manifest configures:
- Bot display name: **Ledgr**
- Slash command: `/ledgr`
- All required bot OAuth scopes
- Socket Mode enabled (switch to HTTP for Cloud Run — see Step 6)
- Event subscriptions: `member_joined_channel`, `message.channels`, `message.groups`, `file_shared`
- Interactivity enabled

---

## Step 2 — Install the app to your workspace

1. In the app settings sidebar, go to **OAuth & Permissions**.
2. Click **Install to Workspace** and approve the requested scopes.
3. Copy the **Bot User OAuth Token** (`xoxb-…`).

---

## Step 3 — Copy tokens into `.env`

Open (or create) `.env` in the project root and add:

```
SLACK_BOT_TOKEN=xoxb-your-token-here
SLACK_SIGNING_SECRET=your-signing-secret-here

# Socket Mode only — create an App-Level Token in Step 3a below
SLACK_APP_TOKEN=xapp-your-app-level-token-here

# GCP (already set if you followed the ADK deploy guide)
PROJECT_ID=your-gcp-project-id
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
LOCATION=asia-southeast1
```

### Step 3a — Create an App-Level Token (Socket Mode only)

1. In the app settings, go to **Basic Information** → **App-Level Tokens**.
2. Click **Generate Token and Scopes**.
3. Give it a name (e.g. `socket-mode`), add the scope **`connections:write`**, and click **Generate**.
4. Copy the token (`xapp-…`) into `SLACK_APP_TOKEN` in `.env`.

The **Signing Secret** is under **Basic Information** → **App Credentials**.

---

## Step 4 — Run with Socket Mode (local / live testing)

Socket Mode requires no public URL — ideal for development:

```bash
uv run python -m app.socket_run
```

You should see:

```
⚡ Ledgr Slack bot connecting via Socket Mode…
```

The bot is now live in your workspace.

---

## Step 5 — Test the bot end-to-end

1. Invite the bot to a channel: `/invite @Ledgr`
2. The bot posts a welcome card with a **Set up client** button.
3. Click the button, fill in the onboarding form (company name, region, FYE month, accounting software, GST) and submit.
4. Drop a PDF invoice or bank statement into the channel.
5. Ledgr processes it (`read_doc` → `build_sheets`) and posts the FY ledger workbook back to the channel.

There is **no** Chart of Accounts spreadsheet upload on the live path (see ADR-0036).

See [docs/qa/light-path-live-smoke.md](qa/light-path-live-smoke.md) for the full manual checklist.

---

## Step 6 — Deploy to Cloud Run (HTTP mode)

### Deploy

```bash
gcloud run deploy ledgr-slack \
  --source . \
  --region asia-southeast1 \
  --allow-unauthenticated \
  --set-env-vars "SLACK_BOT_TOKEN=xoxb-…,SLACK_SIGNING_SECRET=…,PROJECT_ID=…,LOCATION=asia-southeast1"
```

Note the deployed URL, e.g. `https://ledgr-slack-xxxx-as.a.run.app`.

### Switch the manifest to HTTP mode

1. In **Settings** → **Socket Mode**, turn **Socket Mode off**.
2. In **Event Subscriptions**, turn **Enable Events** on and set **Request URL** to:
   ```
   https://<your-service>/slack/events
   ```
3. In **Interactivity & Shortcuts**, set **Request URL** to the same address.
4. Save changes. Slack will send a URL verification challenge — Cloud Run handles it automatically.

> For Cloud Run you do **not** need `SLACK_APP_TOKEN`; set only `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET`.

---

## Multi-workspace (OAuth distribution)

The single-workspace setup above installs the bot into one workspace with a fixed
bot token. To let **many** client workspaces install Ledgr themselves, run it in
OAuth distribution mode. Each install stores its own per-team token in Firestore;
Bolt resolves the right token per request automatically, so no per-client config
or redeploy is needed.

| Variable | Where to get it | Required for |
|---|---|---|
| `SLACK_CLIENT_ID` | Basic Information → App Credentials → Client ID | OAuth distribution |
| `SLACK_CLIENT_SECRET` | Basic Information → App Credentials → Client Secret | OAuth distribution |
| `SLACK_SIGNING_SECRET` | Basic Information → App Credentials → Signing Secret | OAuth distribution |
| `SLACK_BASE_URL` | Public https base of your deployment (Cloud Run URL) | OAuth distribution |
| `SLACK_OAUTH_STATE_SECRET` | Any random secret you generate | OAuth distribution |

When all of `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_SIGNING_SECRET`, and
`SLACK_BASE_URL` are set, the app starts in OAuth mode and exposes the install
routes `GET /slack/install` and `GET /slack/oauth_redirect` (in addition to
`POST /slack/events`). With any of them missing it falls back to single-workspace
HTTP mode.

### Steps

1. **Deploy to Cloud Run** and note the URL:
   ```bash
   gcloud run deploy ledgr \
     --source . \
     --region asia-southeast1 \
     --allow-unauthenticated
   ```
   The deployed URL looks like `https://ledgr-xxxx-as.a.run.app`.

2. **Configure the Slack app from the distribution manifest.** Use
   [`slack/manifest-distributed.json`](../slack/manifest-distributed.json) and
   replace every `https://YOUR_CLOUD_RUN_URL` placeholder with your Cloud Run URL.
   This manifest has Socket Mode **off**, sets the event/interactivity/command
   request URLs to `/slack/events`, and registers the OAuth redirect URL
   `…/slack/oauth_redirect`.

3. **Copy the OAuth credentials.** From **Basic Information → App Credentials**,
   copy the Client ID and Client Secret into `SLACK_CLIENT_ID` /
   `SLACK_CLIENT_SECRET`. Set `SLACK_BASE_URL=https://<cloud-run-url>` and a random
   `SLACK_OAUTH_STATE_SECRET` (e.g. `python -c "import secrets;print(secrets.token_hex(32))"`).

4. **Set those as Cloud Run env vars / Secret Manager and redeploy** so the
   running service picks them up:
   ```bash
   gcloud run deploy ledgr \
     --source . \
     --region asia-southeast1 \
     --allow-unauthenticated \
     --set-env-vars "SLACK_CLIENT_ID=…,SLACK_CLIENT_SECRET=…,SLACK_SIGNING_SECRET=…,SLACK_BASE_URL=https://<cloud-run-url>,SLACK_OAUTH_STATE_SECRET=…,PROJECT_ID=…,LOCATION=asia-southeast1"
   ```
   (Store the secrets in Secret Manager and reference them with `--set-secrets`
   in production.)

4b. **REQUIRED — grant the Cloud Run runtime service account Firestore + GCS access.**
   Without this, `/slack/install` returns **500** (`PermissionDenied` when it writes the
   OAuth state + per-team installation to Firestore) and document archiving fails. Find the
   service account (`gcloud run services describe ledgr --region asia-southeast1
   --format='value(spec.template.spec.serviceAccountName)'`; the default is
   `<PROJECT_NUMBER>-compute@developer.gserviceaccount.com`), then:
   ```bash
   SA=<PROJECT_NUMBER>-compute@developer.gserviceaccount.com
   gcloud projects add-iam-policy-binding <PROJECT_ID> \
     --member="serviceAccount:$SA" --role="roles/datastore.user" --condition=None
   gcloud storage buckets add-iam-policy-binding gs://<BUCKET> \
     --member="serviceAccount:$SA" --role="roles/storage.objectAdmin"
   ```
   IAM takes ~1–2 min to propagate (no redeploy needed). **Best practice:** deploy with a
   dedicated least-privilege `app_sa` (`--service-account`) instead of the default compute SA.

5. **Activate public distribution.** In the Slack app under **Manage
   Distribution**, complete the checklist and click **Activate Public
   Distribution**.

6. **Client install flow.** A client installs Ledgr by visiting
   `https://<cloud-run-url>/slack/install` → **Allow**. Slack redirects back to
   `…/slack/oauth_redirect`, the per-team token is saved to Firestore, and they
   can then run `/ledgr settings` (or the **Set up** button) and drop documents
   in their channel — no further configuration required.

> **Local dev without Cloud Run.** Start a tunnel (e.g. `ngrok http 8080`) and use
> the tunnel URL as `SLACK_BASE_URL`, and as the request/redirect URLs in the
> manifest. The OAuth flow then works against your local server.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `SystemExit: Missing env for Socket Mode: SLACK_BOT_TOKEN, SLACK_APP_TOKEN` | Add the missing vars to `.env` and re-run |
| `invalid_auth` from Slack | Check that `SLACK_BOT_TOKEN` starts with `xoxb-` and was copied in full |
| Bot joins channel but does nothing | Ensure `message.channels` / `message.groups` events are enabled in the manifest |
| Cloud Run returns 403 on `/slack/events` | Slack signature verification failed — check `SLACK_SIGNING_SECRET` |
| Firestore permission denied | Ensure the Cloud Run service account has the **Cloud Datastore User** role (see step 4b) |
| `/slack/install` returns **500** | The runtime SA is missing `roles/datastore.user` — do step 4b, wait ~1–2 min for IAM to propagate |
| Document uploaded but never archived | The runtime SA is missing `roles/storage.objectAdmin` on the bucket — do step 4b |
