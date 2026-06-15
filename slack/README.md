# Slack manifests

| File | Purpose |
| --- | --- |
| `manifest.json` | Production Ledgr app — distributed via OAuth to customer firm workspaces. Deployed alongside Cloud Run. |
| `manifest-dev.json` | "Ledgr (dev)" Slack app for local socket-mode testing. Install in your developer workspace (QBS-AI) only. **Never** install in a customer workspace. |

When editing one, keep scopes / events / slash commands in sync with the other so dev and prod behave identically.

See [`docs/dev-environment.md`](../docs/dev-environment.md) for the full dev/prod setup walkthrough.
