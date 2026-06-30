"""Pydantic schemas for read_doc and billing."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

READ_PROMPT = (
    "You are a factual financial-document extractor. Classify the document and "
    "fill the output schema from what is printed. Read only visible values; "
    "do not infer tax codes or account codes."
)

BUNDLE_READER_INSTRUCTION = (
    "Identify the document type from the page, reconcile line nets to printed "
    "subtotal and header totals when shown, and follow the output schema field "
    "descriptions. When a document has a charge summary plus finer detail or "
    "appendix pages, emit only summary bookable rows that reconcile to the "
    "printed subtotal — do not transcribe supporting detail sub-rows. "
    "When the tax summary splits Standard-Rated and Zero-Rated amounts, bookable "
    "lines follow the tax buckets — not service-category subtotals. On multipage "
    "bills, the printed tax-summary section is authoritative for SR+ZR bucket rows."
)

DocumentKind = Literal["invoice", "receipt", "credit_note", "statement_of_account", "other"]
DocType = Literal["purchase", "sales"]
FileKind = Literal["bank_statement", "commercial_documents"]
LineGrain = Literal["itemized", "summary"]

_READ_DOCUMENT_PROPERTY_ORDER = [
    "doc_type",
    "document_kind",
    "line_grain",
    "vendor_name",
    "customer_name",
    "entity_tax_id",
    "invoice_number",
    "invoice_date",
    "due_date",
    "currency",
    "fx_rate",
    "subtotal",
    "tax_total",
    "grand_total",
    "tax_breakdown",
    "lines",
    "notes",
]


class TaxComponent(BaseModel):
    tax_treatment: str = Field(
        default="",
        description=(
            "Tax treatment label as printed (e.g. 'Standard-Rated 9%', 'Zero-Rated 0%', "
            "'VAT 20%', 'SST 6%'). Read from the page; do not assume."
        ),
    )
    tax_rate_percent: float | None = Field(
        default=None,
        description="Tax rate as printed (9.0, 0.0, 6.0).",
    )
    taxable_amount: float | None = Field(
        default=None,
        description="Net amount this treatment applies to.",
    )
    tax_amount: float | None = Field(
        default=None,
        description="Tax amount for this treatment.",
    )


class Line(BaseModel):
    description: str = Field(
        default="",
        description=(
            "Bookable charge label for one ledger row. "
            "When line_grain is summary and tax_breakdown has multiple treatments (SR + ZR), "
            "emit one line per tax bucket. For telecommunications bills set description to "
            "exactly 'Telephone charges (SR)' or 'Telephone charges (ZR)' — only when SR and "
            "ZR buckets are both present; not for single-GST bills. Do not use service "
            "categories (Internet/Mobile/Switch) when SR+ZR tax buckets are printed. "
            "Otherwise use the printed row label (category, service group, line item, or "
            "statement_of_account invoice reference such as 'IA-001')."
        ),
    )
    quantity: float | None = Field(
        default=None,
        description=(
            "Printed quantity for this line when a product table shows a qty column. "
            "Fill for itemized invoice rows; omit on summary charge-category rows."
        ),
    )
    unit_amount: float | None = Field(
        default=None,
        description=(
            "Printed unit or net price per unit (before tax) when shown. "
            "Fill for itemized invoice rows; omit on summary charge-category rows."
        ),
    )
    net_amount: float | None = Field(
        default=None,
        description=(
            "Net amount for this bookable row; must contribute to the document subtotal. "
            "Required when the line shows a net or line total. "
            "Must equal quantity * unit_amount for unit-priced lines."
        ),
    )
    tax_amount: float | None = Field(
        default=None,
        description=(
            "Printed tax (GST/VAT/SST) for this line. Use 0 for zero-rated or exempt "
            "lines. Required on Standard-Rated tax-bucket rows when the tax summary "
            "prints a positive tax for that bucket."
        ),
    )
    total_amount: float | None = Field(
        default=None,
        description="Printed line grand total (net + tax) for this line.",
    )
    tax_treatment: str = Field(
        default="",
        description=(
            "Tax treatment label printed for this line (e.g. 'Standard-Rated 9%', "
            "'Zero-Rated 0%'). Required on tax-bucket summary rows and when the line "
            "shows G/Z/SR/ZR on the page. When only a single-letter code is printed, "
            "map G → 'Standard-Rated 9%' and Z → 'Zero-Rated 0%'. Empty when the line "
            "shows no treatment."
        ),
    )


class ReadDocument(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"propertyOrdering": _READ_DOCUMENT_PROPERTY_ORDER},
    )

    doc_type: DocType = Field(
        default="purchase",
        description=(
            "'purchase' when we are the Bill-To party paying a supplier; "
            "'sales' when we issued the document to our customer. "
            "Decide from issuer vs Bill-To layout, not the document title."
        ),
    )
    document_kind: DocumentKind = Field(
        default="invoice",
        description=(
            "The type you identify from the page: 'invoice' for invoices, tax invoices, "
            "and bills; 'receipt' for payment receipts; 'credit_note' for credit notes or "
            "credit memos that reverse or adjust a prior invoice; 'statement_of_account' for a "
            "debtor statement / statement of account that lists invoice numbers and "
            "amounts without line items — use only when no full invoices are attached "
            "in the same file; 'other' for anything else. "
            "Pick from what is printed — do not assume."
        ),
    )
    line_grain: LineGrain = Field(
        default="itemized",
        description=(
            "Commit to the grain of lines[] before filling it. "
            "'itemized' when the document is a standard invoice or credit note with a "
            "product/service table and no higher charge-summary section — keep every "
            "distinct printed charge row. "
            "'summary' when the document shows a charge summary or tax summary and finer "
            "rows are supporting detail only (usage logs, call lists, package add-ons, "
            "appendix pages) — emit only summary rows whose nets reconcile to subtotal. "
            "When summary and the printed tax summary shows more than one treatment "
            "(e.g. Standard-Rated 9% + Zero-Rated 0%), lines[] should be the "
            "tax-bucket split (one row per treatment), not charge-category rows."
        ),
    )
    vendor_name: str = Field(
        default="",
        description="Name of the issuer / supplier (the party sending the bill or receipt).",
    )
    customer_name: str = Field(
        default="",
        description="Name of the bill-to / customer (the party being asked to pay).",
    )
    entity_tax_id: str = Field(
        default="",
        description="GST/VAT/SST/tax registration number of the issuer if printed.",
    )
    invoice_number: str = Field(
        default="",
        description=(
            "Printed invoice, bill, receipt, credit note, or statement reference number "
            "(e.g. invoice no., bill no., credit note no.). "
            "For statement_of_account, use the debtor account number or "
            "statement reference when shown."
        ),
    )
    invoice_date: str = Field(
        default="",
        description="Printed document date, normalised to ISO YYYY-MM-DD.",
    )
    due_date: str = Field(
        default="",
        description="Printed due date, normalised to ISO YYYY-MM-DD. Empty when not shown.",
    )
    currency: str = Field(
        default="",
        description="Three-letter currency code printed on the document (SGD, MYR, USD, ...).",
    )
    fx_rate: float | None = Field(
        default=None,
        description="Exchange rate to base currency if printed; null otherwise.",
    )
    subtotal: float | None = Field(
        default=None,
        description=(
            "Sum of line net_amounts, or the printed subtotal when shown. "
            "For statement_of_account, use the balance due or sum of listed amounts."
        ),
    )
    tax_total: float | None = Field(
        default=None,
        description="Sum of line tax_amounts, or the printed tax total when shown.",
    )
    grand_total: float | None = Field(
        default=None,
        description="Printed grand total or amount due. Should equal subtotal + tax_total.",
    )
    tax_breakdown: list[TaxComponent] = Field(
        default_factory=list,
        description=(
            "One entry per distinct tax treatment printed on the document tax summary. "
            "Fill whenever a tax summary section is printed (including multipage bills "
            "where the tax summary appears on a summary page). Each entry: tax_treatment "
            "label as printed, tax_rate_percent, taxable_amount, tax_amount. "
            "On summary bills with multiple treatments, this is the authoritative "
            "bookable split and lines[] should align one row per treatment — charge-category "
            "rows on other pages are not bookable when SR+ZR buckets are printed. "
            "On itemized invoices, still fill from the printed tax summary when shown. "
            "Empty when the document shows a single tax line or no breakdown."
        ),
    )
    lines: list[Line] = Field(
        default_factory=list,
        description=(
            "Bookable charge rows for ledger posting, in printed order. Follow line_grain. "
            "When itemized, include each distinct printed charge row with qty, unit price, "
            "and line total as shown — do not collapse. "
            "When summary with only one tax treatment, include summary-level charge-category "
            "rows (e.g. Internet/Mobile/Switch) whose net amounts reconcile to subtotal — "
            "never appendix, usage-log, or package-detail sub-rows. "
            "When summary and tax_breakdown has multiple treatments, lines[] must contain "
            "exactly one row per tax_breakdown entry (typically two: SR and ZR) — no "
            "charge-category or appendix rows. This tax-bucket mode applies only when "
            "tax_breakdown has two or more distinct treatments; when only one GST rate "
            "is printed, use charge-category summary rows instead. net_amount and "
            "tax_amount from the printed tax summary; tax_treatment as printed; "
            "description per Line field rules. "
            "Empty only for header-only memos."
        ),
    )
    notes: str = Field(
        default="",
        description=(
            "Brief extraction caveats only (e.g. ignored appendix or detail sections, "
            "reconciliation mismatch); empty string when clean."
        ),
    )


class BankTxn(BaseModel):
    date: str | None = Field(
        default=None,
        description="Transaction date as ISO YYYY-MM-DD when visible on the statement row.",
    )
    description: str = Field(
        default="",
        description="Transaction narrative exactly as printed on the row.",
    )
    bank_ref: str | None = Field(
        default=None,
        description="Cheque or bank reference if shown on the row.",
    )
    withdrawal: float | None = Field(
        default=None,
        description="Debit / paid out amount (positive number).",
    )
    deposit: float | None = Field(
        default=None,
        description="Credit / received amount (positive number).",
    )
    balance: float | None = Field(
        default=None,
        description="Running balance after this transaction row.",
    )


class BankAccount(BaseModel):
    bank_name: str = Field(
        default="",
        description=(
            "Bank label only (e.g. 'OCBC' or 'DBS Bank Ltd'). "
            "Do NOT embed account digits in this field."
        ),
    )
    account_number: str | None = Field(
        default=None,
        description="Account number as printed on the statement.",
    )
    currency: str | None = Field(
        default=None,
        description="ISO currency code for this account (default SGD when not shown).",
    )
    statement_period: str | None = Field(
        default=None,
        description="Printed statement period, e.g. '01 DEC 2025 - 31 DEC 2025'.",
    )
    opening_balance: float | None = Field(
        default=None,
        description="Brought-forward / opening balance (not a transaction row).",
    )
    closing_balance: float | None = Field(
        default=None,
        description="Final closing balance printed on the statement.",
    )
    transactions: list[BankTxn] = Field(
        default_factory=list,
        description="Every transaction row in printed order. Opening balance is not a row.",
    )


class ReadBankStatement(BaseModel):
    accounts: list[BankAccount] = Field(
        default_factory=list,
        description="One entry per distinct (account_number, currency) section.",
    )


class ReadDocumentBundle(BaseModel):
    file_kind: FileKind = Field(
        description=(
            "Decide from the content: 'bank_statement' for statements with transaction rows "
            "and balances; 'commercial_documents' for invoices, receipts, and bills."
        ),
    )
    accounts: list[BankAccount] = Field(
        default_factory=list,
        description="Populated when file_kind is bank_statement; empty otherwise.",
    )
    documents: list[ReadDocument] = Field(
        default_factory=list,
        description=(
            "One entry per logical commercial document when file_kind is commercial_documents. "
            "When a debtor statement page is followed by full invoices or credit notes, return "
            "each as its own document with itemized lines and do not include the statement "
            "page in this array. When the file contains only the statement listing with no "
            "attached full invoices, return one document with "
            "document_kind='statement_of_account' and listed invoice references as lines. "
            "Empty for bank statements."
        ),
    )
    document_count: int = Field(
        default=0,
        description="Number of commercial documents (must equal len(documents)).",
    )


CreditStatus = Literal["not_checked", "estimated", "charged", "blocked", "not_billable"]


class CreditSummary(BaseModel):
    """Billing summary attached to every batch result."""

    credits_estimated: int = 0
    credits_used: int = 0
    credits_remaining: int | None = None
    credit_status: CreditStatus = "not_checked"
    credit_ledger_refs: list[str] = Field(default_factory=list)
