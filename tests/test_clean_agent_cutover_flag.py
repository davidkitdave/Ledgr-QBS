import os  # noqa: F401 — env var shadow; matches the brief verbatim


def test_clean_agent_flag_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("LEDGR_USE_CLEAN_AGENT", raising=False)
    from accounting_agents.slack_runner import _use_clean_agent

    assert _use_clean_agent() is False


def test_clean_agent_flag_on(monkeypatch) -> None:
    monkeypatch.setenv("LEDGR_USE_CLEAN_AGENT", "1")
    from accounting_agents.slack_runner import _use_clean_agent

    assert _use_clean_agent() is True
