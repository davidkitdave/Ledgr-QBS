# Dev environment setup

How to run the local socket-mode bot against your QBS-AI workspace without affecting real customers on the Cloud Run production deployment.

## Architecture

Two completely separate Slack apps share one codebase but have different tokens, different Firestore namespaces, and different model defaults:

- **Ledgr (dev)** — local socket-mode process, `LEDGR_ENV=dev`, AI Studio key, `gemini-2.5-flash-lite` for invoices. Installed in QBS-AI workspace only for testing.
- **Ledgr (prod)** — Cloud Run HTTP mode, `LEDGR_ENV=prod`, Vertex AI (asia-southeast1), both model tiers default to `gemini-2.5-flash` (until flash-lite reaches that region). Installed in customer workspaces via OAuth.

The two apps share zero state: different bot tokens, different Firestore namespaces (or ideally separate GCP projects), different model env vars.

## Steps

### 1. Create the "Ledgr (dev)" Slack app

1. Go to https://api.slack.com/apps → **Create New App** → **From a manifest**
2. Pick **QBS-AI** as the workspace
3. Paste the contents of `slack/manifest-dev.json`
4. Install to QBS-AI
5. Copy the Bot User OAuth Token (`xoxb-…`) → `SLACK_BOT_TOKEN`
6. Under **Basic Information** → **App-Level Tokens**, generate a token with `connections:write` scope (`xapp-…`) → `SLACK_APP_TOKEN`
7. Copy the Signing Secret → `SLACK_SIGNING_SECRET`

### 2. Configure `.env`

```sh
cp .env.example .env
```

Fill in the three Slack token vars from step 1, your Google AI Studio key, and your dev GCP project id. For Firestore isolation choose one of:

- **Separate GCP project (recommended):** point `GOOGLE_CLOUD_PROJECT` at a dev-only project. Leave `LEDGR_FIRESTORE_NAMESPACE` unset.
- **Shared GCP project:** set `LEDGR_FIRESTORE_NAMESPACE=dev` — collections become `dev_clients`, `dev_channels`, etc. so dev writes never collide with prod.

### 3. Run the bot

```sh
.venv/bin/python slack_bot.py
```

The process connects to QBS-AI via socket mode. Status messages in Slack will be prefixed with `[dev]` so you can tell at a glance that Ledgr-dev replied, not the production bot.

### 4. Invite Ledgr-dev to a test channel

```
/invite @Ledgr-dev
```

Only invite Ledgr-dev to channels like `#qa-blockkit` or personal DMs. Never invite it to a channel where the production bot is already present or where real client data lives.

## Production

Cloud Run runs the **other** Slack app (`Ledgr`, `slack/manifest.json`) with production tokens from Secret Manager and `LEDGR_ENV=prod`. See `.env.prod.example` for the full variable reference — that file is documentation only; Cloud Run never reads it directly.

## Switching back

You don't need to switch. Keep both running simultaneously: local Ledgr-dev for iteration, Cloud Run prod for real customers. The separate bot tokens guarantee the two apps never respond to each other's events.
