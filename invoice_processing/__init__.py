"""Invoice Processing -- deterministic pipeline + (legacy) learning agent.

The deterministic engine (``invoice_processing.pipeline`` and the
``classify`` / ``extract`` / ``export`` packages) is what the live Slack app
uses. The heavy LlmAgent in ``agent.py`` (Acting / Investigation / ALF +
rule-learning) solves a *different* problem and is NOT on the live path.

It is imported **lazily** (PEP 562) so that importing the pipeline does not drag
in the legacy machinery -- importing ``invoice_processing.pipeline`` no longer
loads ``agent.py``. The package-level ``root_agent`` attribute still resolves on
demand for backward compatibility (e.g. ``adk web``):

    from invoice_processing import root_agent   # still works, loaded on access
"""


def __getattr__(name):  # PEP 562 lazy module attribute
    if name == "root_agent":
        from .agent import root_agent

        return root_agent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
