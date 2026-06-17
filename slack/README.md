# Slack manifests

Two first-class manifests — paste into [api.slack.com/apps](https://api.slack.com/apps) → **App Manifest** → Save → **Reinstall to Workspace**.

| File | Slack app | Slash command | Runtime |
| --- | --- | --- | --- |
| **`manifest-qbs.json`** | **Ledgr-QBS** (production) | `/ledgr` | Cloud Run HTTP (`ledgr-…run.app`) |
| **`manifest-dev.json`** | **Ledgr (dev)** | `/ledgr-dev` | Local socket mode (`slack_bot.py`) |

Using **`/ledgr-dev`** on the dev app prevents the workspace conflict Slack warns about when both apps are installed in QBS-AI.

## Ledgr-QBS (production)

1. Open your **Ledgr-QBS** app at api.slack.com.
2. **App Manifest** → paste `slack/manifest-qbs.json` → **Save Changes**.
3. **OAuth & Permissions** → confirm `reactions:write` is listed → **Reinstall to Workspace**.
4. Cloud Run already points at the URLs in the manifest; no token copy unless you use a static bot token.

## Ledgr (dev)

1. Open **Ledgr (dev)** at api.slack.com.
2. **App Manifest** → paste `slack/manifest-dev.json` → **Save Changes**.
3. **Reinstall to Workspace**.
4. Copy the new **Bot User OAuth Token** → `.env` `SLACK_BOT_TOKEN`.
5. Ensure `LEDGR_ENV=dev` (default) and restart `slack_bot.py`.
6. Use **`/ledgr-dev settings`**, **`/ledgr-dev export`**, etc. — not `/ledgr`.

The local bot registers the command from `ledgr_slash_command_name()` (`app/commands.py`), which returns `/ledgr-dev` when `LEDGR_ENV=dev`.

## Other files (templates)

| File | Purpose |
| --- | --- |
| `manifest.json` | Generic socket-mode template (legacy name "Ledgr") |
| `manifest-ready.json` | Same as `manifest-qbs.json` but display name "Ledgr" — prefer `manifest-qbs.json` |
| `manifest-distributed.json` | Placeholder URLs for new OAuth deployments |

Keep **bot scopes** and **bot events** in sync between QBS and dev except where dev needs extra events (`message.im`, `app_mention`) for socket-mode chat QA.

See [`docs/dev-environment.md`](../docs/dev-environment.md) for the full dev/prod setup.
