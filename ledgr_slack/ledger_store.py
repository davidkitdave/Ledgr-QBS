"""Slack FY ledger workbook store."""

from ledgr_slack.ledger_store_append import _SlackLedgerStoreAppendMixin
from ledgr_slack.ledger_store_base import SlackLedgerStoreBase
from ledgr_slack.ledger_store_mutate import _SlackLedgerStoreMutateMixin

class SlackLedgerStore(
    _SlackLedgerStoreMutateMixin,
    _SlackLedgerStoreAppendMixin,
    SlackLedgerStoreBase,
):
    """Append rows to the channel-hosted FY ledger workbook."""

    pass
