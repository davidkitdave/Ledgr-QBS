"""Single source of truth for hard-violation IDs used across the policy layer.

These IDs are emitted by:
- ``ledgr_agent.tools.document_tools._run_policy_validators``
- ``ledgr_agent.callbacks.validate_output``

Any new hard-violation ID MUST be added here first so both producer and
consumer stay in sync automatically.
"""

HARD_VIOLATION_IDS: frozenset[str] = frozenset(
    {
        "gst_claimed_by_non_registered_client",
        "policy_validator_error",
        "invalid_tax_code",
    }
)
