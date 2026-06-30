"""Backward-compatible shim — use ``python -m ledgr_slack`` instead."""

from ledgr_slack.app import main

if __name__ == "__main__":
    main()
