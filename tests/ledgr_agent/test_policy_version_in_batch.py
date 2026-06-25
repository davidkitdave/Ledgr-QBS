from ledgr_agent.tools.batch_mapper import map_engine_batch_to_contract
from invoice_processing.pipeline import BatchResult as EngineBatchResult


class _Client:
    client_id = "playground"
    firm_id = "team_demo"
    region = "SG"


def test_validation_summary_includes_policy_version() -> None:
    engine = EngineBatchResult(docs=[], errors=[], workbooks={})
    batch = map_engine_batch_to_contract(
        engine,
        client=_Client(),
        source_files=[],
        missing_files=[],
        tax_policy_version="sg-2026-01",
    )
    assert batch.validation_summary["tax_policy_version"] == "sg-2026-01"
