"""Slack frontend package for Ledgr."""

__all__ = ["build_fastapi_app"]


def __getattr__(name: str):
    if name == "build_fastapi_app":
        from ledgr_slack.app import build_fastapi_app

        return build_fastapi_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
