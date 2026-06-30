"""Pytest configuration for Ledgr unit tests."""

import os
import sys
from pathlib import Path

# Pin the Firestore namespace to empty BEFORE load_dotenv so .env cannot
# override it.  Tests that exercise namespace behaviour set/unset the var
# explicitly via ``monkeypatch``.  This must come before any import that
# triggers accounting_agents.config (which also calls load_dotenv).
os.environ["LEDGR_FIRESTORE_NAMESPACE"] = ""

from dotenv import load_dotenv  # noqa: E402

# Load local environment variables (will not override the pinned var above).
load_dotenv()

# Ensure the agent package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

