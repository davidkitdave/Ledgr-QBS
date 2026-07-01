"""Pytest configuration for Ledgr unit tests."""

import os
import sys
from pathlib import Path

pytest_plugins = ["tests._slack_test_helpers"]

# Pin the Firestore namespace to empty BEFORE load_dotenv so .env cannot
# override it.  Tests that exercise namespace behaviour set/unset the var
# explicitly via ``monkeypatch``.  This must come before any import that
# triggers ledgr_slack.config (which also calls load_dotenv).
os.environ["LEDGR_FIRESTORE_NAMESPACE"] = ""

from dotenv import load_dotenv  # noqa: E402

# Load local environment variables (will not override the pinned var above).
load_dotenv()

# Ensure the agent package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def pytest_collection_modifyitems(config, items):
    """Auto-mark bank-formula tests in test_ledger_store as slow."""
    import pytest

    for item in items:
        if "test_ledger_store.py" in str(item.fspath) and item.name.startswith("test_bank"):
            item.add_marker(pytest.mark.slow)

