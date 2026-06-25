"""Session-state keys and write-tool constants for the chat assistant."""

from __future__ import annotations


from accounting_agents.jurisdiction import (
    REGION_MALAYSIA,
    REGION_SINGAPORE,
    registration_threshold_for_region,
)

#: The session state key the runner must set before routing to the chat path.
LEDGER_DATA_KEY = "ledger_data"

#: The session state key the chat write tools append confirmed write specs to.
#: The Slack runner drains this AFTER a chat turn, executing each spec against
#: the workbook (the tools never do network I/O themselves — see ADR-0009).
PENDING_WRITE_KEY = "pending_ledger_write"

#: The session state key the learn_mapping tool appends mapping specs to.
#: The Slack runner drains this AFTER a chat turn, calling
#: ``client_store.add_correction`` for each entry (the tool never does I/O).
PENDING_LEARN_KEY = "pending_learn_mapping"

#: The session state key the ``re_extract_document`` tool appends re-extract
#: specs to (ADR-0010). The Slack runner drains this AFTER a chat turn, re-running
#: the document pipeline (with the hint + ``replace=True``) per spec via
#: ``process_file_event`` — the tool itself never downloads or runs anything.
PENDING_REEXTRACT_KEY = "pending_reextract"

#: Recent document-processing deliveries injected by the Slack runner (ADR chat introspection).
PROCESSING_LOG_KEY = "processing_log"

#: Pending HITL reviews for the current channel. Injected by the runner from
#: ``hitl.list_pending_interrupts`` so the chat agent can answer "anything
#: waiting on my approval?" without doing its own Firestore I/O.
PENDING_REVIEWS_KEY = "pending_reviews"

#: Per-document session snapshot for files referenced in the processing log.
#: Injected by the runner via ``_snapshot_doc_sessions``; the chat tools
#: treat this as read-only introspection data only.
DOCUMENT_SESSIONS_KEY = "document_sessions"

#: Last invoice/account-code focus for thread follow-ups (set by the runner
#: after a direct account-code answer or ``explain_posted_line``).
THREAD_FOCUS_KEY = "thread_focus"



#: Invoice sheets the write tools may mutate. Bank sheets carry a derived running
#: balance (memory ``bank-ledger-continuous-sorted``) so amending/removing one
#: would desync the chain — the tools refuse with a clear message instead.
_INVOICE_SHEETS: frozenset[str] = frozenset({"Purchase", "Sales"})

#: The accounting software whose workbooks the chat AMEND/edit tools may write to.
#: This gates ONLY in-chat editing of an existing workbook — NOT export. Xero is
#: fully supported for EXPORT (see ``XeroLedgerExporter`` / ``EXPORTERS``): we
#: generate correct Xero import rows from scratch. But the amend tools edit existing
#: rows by QBS column header (``_EDITABLE_FIELD_HEADERS``); Xero uses different
#: headers (``*AccountCode``, ``TaxAmount`` no-space), so amending a Xero workbook
#: through the QBS-shaped edit path would silently write wrong tax dollars or raise
#: "unknown column" errors. Keep the amend gate until the amend tools are made
#: column-aware per software (deliberate safety guard, not rigid over-control —
#: WS4.3 decision 2026-06-19).
_SUPPORTED_WRITE_SOFTWARE: frozenset[str] = frozenset({"QBS Ledger", "qbs"})

#: User-facing amend field → the canonical workbook column header (QBS Ledger).
#: ``tax`` is handled specially (it re-derives the QBS ``Tax Amount`` via the
#: classifier, never a free-text write), so it is intentionally absent here.
_EDITABLE_FIELD_HEADERS: dict[str, str] = {
    "account": "Account Code / COA",
    "account code": "Account Code / COA",
    "coa": "Account Code / COA",
    "amount": "Source Amount",
    "net amount": "Source Amount",
    "description": "Description",
}

#: Field aliases that mean "amend the tax treatment". These pass THROUGH the
#: §0.5-C master gate (a non-registered client is forced to NT) rather than
#: writing the user's literal text.
_TAX_FIELD_ALIASES: frozenset[str] = frozenset(
    {"tax", "tax rate", "tax treatment", "tax code", "tax type"}
)

#: Dollar-amount tax headers that the QBS layout uses.  ``TaxAmount`` (no
#: space) is the Xero column — kept here defensively so a mixed workbook never
#: leaves a stale dollar value, but QBS clients are already gated above.
#: ``Tax Rate`` / ``tax_rate`` are NOT live workbook columns and are removed
#: to avoid writing a raw canonical treatment code into a wrong cell.
_TAX_AMOUNT_HEADERS: frozenset[str] = frozenset({"Tax Amount", "TaxAmount"})
#: Code-carrying headers rewritten from the re-classified treatment.
#: Only ``*TaxType`` (Xero) remains; dead ``Tax Rate``/``tax_rate`` entries removed.
_TAX_CODE_HEADERS: frozenset[str] = frozenset({"*TaxType"})

#: Columns used to build a row SIGNATURE for replay-safety (HIGH-2).  The
#: signature is a stable hash of key identifying values captured at Turn-1 so
#: the commit branch can detect that the row shifted or was edited since the
#: user saw the proposal.
_SIGNATURE_COLS: tuple[str, ...] = (
    "Description", "Source Amount", "Account Code / COA", "Tax Amount",
)

#: Legacy re-exports — canonical values live in jurisdiction YAML ``registration_threshold``.
GST_THRESHOLD_SGD, _, _ = registration_threshold_for_region(REGION_SINGAPORE)
SST_THRESHOLD_MYR, _, _ = registration_threshold_for_region(REGION_MALAYSIA)
