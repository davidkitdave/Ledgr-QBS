"""Socket Mode runner — easiest path for live local testing (no public URL needed).

Usage:
    uv run python -m app.socket_run

Requires SLACK_BOT_TOKEN and SLACK_APP_TOKEN (xapp-…) in the environment.
See docs/slack-setup.md for how to obtain these tokens.
"""

from __future__ import annotations

from app.config import get_settings, missing_slack_socket


def run() -> None:
    """Start the Slack bot in Socket Mode.

    Raises SystemExit with a helpful message when required env vars are absent
    so the user knows exactly what to set without reading a stack trace.
    """
    s = get_settings()
    miss = missing_slack_socket()
    if miss:
        raise SystemExit(
            "Missing env for Socket Mode: "
            + ", ".join(miss)
            + " — see docs/slack-setup.md"
        )

    from slack_bolt.adapter.socket_mode import SocketModeHandler

    from app.slack_app import build_app
    from invoice_processing.export.client_context import FirestoreClientStore

    bolt_app = build_app(
        store=FirestoreClientStore(),
        bot_token=s.slack_bot_token,
        signing_secret=s.slack_signing_secret or "placeholder",
    )
    print("⚡ Ledgr Slack bot connecting via Socket Mode…")
    SocketModeHandler(bolt_app, s.slack_app_token).start()


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(".env")
    run()
