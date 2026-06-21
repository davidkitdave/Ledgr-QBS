"""Bank / account statement extraction via Gemini Flash — hybrid digital + vision.

Turns a bank statement (PDF or image bytes) into a structured `ExtractedBankStatement`, then
maps each account to an export-layer `BankStatement` and runs a running-balance reconciliation.

Three things set this lane apart from the invoice lane:
- HYBRID extraction: a digital PDF (real text layer) is read with pdfplumber and sent to Gemini
  as text (cheaper, exact characters); a scanned/image-only PDF falls back to a multimodal
  (vision) call on the raw bytes.
- MULTI-ACCOUNT / MULTI-CURRENCY: one statement can hold several accounts, or one account with
  separate per-currency sections — each becomes its own `ExtractedAccount` / `BankStatement`.
- RUNNING-BALANCE RECONCILIATION: every transaction is walked to confirm
  prev - withdrawal + deposit == balance. EVERY row is kept, never summarized or merged.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Optional, Union

from google.genai import types
from pydantic import BaseModel, Field

from ..export.models import BankStatement, BankTransaction
from ..shared_libraries.genai_client import lite_model, make_client, std_model
from .invoice_extractor import _parse_date, mime_for


class ExtractedBankTxn(BaseModel):
    date: Optional[str] = Field(None, description="ISO date YYYY-MM-DD if determinable")
    description: str = Field(description="Transaction description / narrative")
    bank_ref: Optional[str] = Field(None, description="Bank reference / cheque no. if shown")
    withdrawal: Optional[float] = Field(None, description="Amount debited / paid out (positive)")
    deposit: Optional[float] = Field(None, description="Amount credited / received (positive)")
    balance: Optional[float] = Field(None, description="Running account balance after this txn")


class ExtractedAccount(BaseModel):
    bank_name: str = Field(description="Bank label only (e.g. 'OCBC' or 'DBS Bank Ltd'); do NOT pack account digits into this field")
    account_number: Optional[str] = None
    currency: Optional[str] = Field(None, description="ISO currency code, default SGD")
    statement_period: Optional[str] = Field(None, description="As printed, e.g. '01 DEC 2024 - 31 DEC 2024'")
    opening_balance: Optional[float] = Field(None, description="Brought-forward / opening balance")
    closing_balance: Optional[float] = Field(None, description="Final balance")
    transactions: list[ExtractedBankTxn] = Field(default_factory=list)


class ExtractedBankStatement(BaseModel):
    accounts: list[ExtractedAccount] = Field(default_factory=list)


_PROMPT = """You are extracting a bank / account statement for a Singapore/Malaysia bookkeeping ledger.

Extract a bank/account statement into one or more accounts. Produce ONE account entry per distinct
(account number, currency). If the statement covers multiple accounts, or one account shows separate
per-currency sections (e.g. a multi-currency account with SGD/USD/EUR portions), SPLIT each into its
own account entry with its own opening balance, transactions, and running balance.

For each account, extract EVERY transaction row in order — do NOT summarize, skip, or merge rows.
Read all pages.

Per transaction row:
- withdrawal = amount paid out (positive); deposit = amount received (positive); exactly one per row.
  balance = running balance shown after that transaction.
- Capture the opening/brought-forward balance as opening_balance ('Balance B/F' /
  'Balance Brought Forward'). Do NOT emit it as a transaction row. closing_balance = final balance.
- date in ISO YYYY-MM-DD. bank_ref = any cheque/reference number if shown.
- description = the transaction narrative as printed.

Per account:
- bank_name = the bank label ONLY (e.g. 'OCBC' or 'DBS Bank Ltd'). Do NOT pack
  account digits into the bank_name — the tab title is built in code from
  ``bank_name`` + ``account_number`` + ``currency`` (format: 'Bank - XXXX - CCY'),
  so duplicating the digits here causes multi-currency sections to collapse
  into a single Excel tab.
- currency as ISO (default SGD). statement_period as printed. account_number
  as printed on the statement, including any dashes (e.g. '072-955554-5').

Do not invent values; leave a field null if it is not visible."""


def _digital_text(path: Union[str, Path, io.IOBase, None] = None, *, pdf_bytes: Optional[bytes] = None) -> str:
    """Concatenate the digital text layer of a PDF (page text + table rows) via pdfplumber.

    Accepts either a file-system path (``path``) or raw PDF bytes (``pdf_bytes``).
    When both are supplied ``path`` takes precedence (avoids an extra read).
    """
    import pdfplumber

    if path is not None:
        source = path
    elif pdf_bytes is not None:
        source = io.BytesIO(pdf_bytes)
    else:
        raise ValueError("_digital_text requires either path or pdf_bytes")

    parts: list[str] = []
    with pdfplumber.open(source) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text:
                parts.append(text)
            for table in page.extract_tables() or []:
                for row in table:
                    cells = [c for c in row if c]
                    if cells:
                        parts.append(" | ".join(str(c) for c in cells))
    return "\n".join(parts)


def _is_digital(path: Union[str, Path, None] = None, *, pdf_bytes: Optional[bytes] = None) -> bool:
    """True if the PDF has a meaningful digital text layer (>=200 chars and some digits).

    Works from a file-system path or from raw bytes (uses ``io.BytesIO`` internally).
    """
    try:
        text = _digital_text(path, pdf_bytes=pdf_bytes)
    except Exception:
        return False
    stripped = text.strip()
    if len(stripped) < 200:
        return False
    return sum(c.isdigit() for c in stripped) >= 5


def _extract_digital(text: str, *, model: Optional[str] = None,
                     project: Optional[str] = None, location: Optional[str] = None) -> ExtractedBankStatement:
    """Structured-output call with the extracted TEXT as content (text-only, no image Part)."""
    client = make_client(project, location)
    model = model or lite_model()
    resp = client.models.generate_content(
        model=model,
        contents=[text, _PROMPT],
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=ExtractedBankStatement,
        ),
    )
    return ExtractedBankStatement.model_validate_json(resp.text)


def _extract_vision(data: bytes, mime_type: str, *, model: Optional[str] = None,
                    project: Optional[str] = None, location: Optional[str] = None) -> ExtractedBankStatement:
    """Multimodal structured-output call on the raw document bytes (for scanned PDFs/images)."""
    client = make_client(project, location)
    model = model or std_model()
    part = types.Part.from_bytes(data=data, mime_type=mime_type)
    resp = client.models.generate_content(
        model=model,
        contents=[part, _PROMPT],
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=ExtractedBankStatement,
        ),
    )
    return ExtractedBankStatement.model_validate_json(resp.text)


def _has_data(ex: ExtractedBankStatement) -> bool:
    return bool(ex.accounts) and any(a.transactions for a in ex.accounts)


def extract_bank_statement(
    data: bytes,
    mime_type: str,
    *,
    path: Optional[str | Path] = None,
    mode: str = "auto",
    model: Optional[str] = None,
    digital_model: Optional[str] = None,
    vision_model: Optional[str] = None,
    project: Optional[str] = None,
    location: Optional[str] = None,
) -> tuple[ExtractedBankStatement, str]:
    """Extract a bank statement; returns (parsed, mode_used).

    Model routing (when callers do not override):
    - **digital** (pdfplumber text path) → ``lite_model()`` / flash-lite
    - **vision** (scanned PDF or image) → ``std_model()`` / flash

    ``model`` sets both paths when ``digital_model`` / ``vision_model`` are unset.

    mode:
    - 'auto'    -> pick digital vs vision via _is_digital. Works from either a
                   file-system ``path`` OR directly from the raw PDF ``data`` bytes
                   (pdfplumber.open accepts an io.BytesIO — no temp file needed).
                   If pdfplumber text is empty or the digital call yields no accounts
                   / transactions, falls back to the vision (multimodal) call.
                   Non-PDF mime types always use vision.
    - 'digital' -> force the pdfplumber-text path (requires ``path``).
    - 'vision'  -> force the multimodal/bytes path.
    """
    dig_kw = dict(
        model=digital_model or model,
        project=project,
        location=location,
    )
    vis_kw = dict(
        model=vision_model or model,
        project=project,
        location=location,
    )

    if mode == "vision":
        return _extract_vision(data, mime_type, **vis_kw), "vision"

    if mode == "digital":
        if path is None:
            raise ValueError("mode='digital' requires a path for pdfplumber text extraction")
        return _extract_digital(_digital_text(path), **dig_kw), "digital"

    # mode == 'auto'
    # Digital detection works from either a path or from the raw bytes directly
    # (pdfplumber.open accepts an io.BytesIO, so no temp file is needed).
    if path is not None:
        is_dig = _is_digital(path)
        text_src_kw: dict = {"path": path}
    elif mime_type == "application/pdf":
        is_dig = _is_digital(pdf_bytes=data)
        text_src_kw = {"pdf_bytes": data}
    else:
        # Non-PDF bytes (image): vision only.
        is_dig = False
        text_src_kw = {}

    if is_dig:
        text = _digital_text(**text_src_kw).strip()
        if text:
            ex = _extract_digital(text, **dig_kw)
            if _has_data(ex):
                return ex, "digital"
        # empty text or no usable data -> fall back to vision
        return _extract_vision(data, mime_type, **vis_kw), "vision"

    return _extract_vision(data, mime_type, **vis_kw), "vision"


def extract_bank_file(path: str | Path, *, mode: str = "auto", **kw) -> tuple[ExtractedBankStatement, str]:
    path = Path(path)
    return extract_bank_statement(path.read_bytes(), mime_for(path), path=path, mode=mode, **kw)


def reconcile_running_balance(
    stmt: BankStatement, *, tol_abs: float = 0.01, tol_rel: float = 0.001
) -> tuple[bool, str]:
    """Walk transactions in order and verify the running balance ties out.

    For each txn with non-None balance and non-None prev: expected = prev - withdrawal + deposit;
    txn.math_ok = |balance - expected| <= max(tol_abs, tol_rel*|expected|); advance prev=balance.
    When prev or balance is None, math_ok=None (cannot check). Counts the False rows, sets
    stmt.reconciled and stmt.reconcile_note. Returns (reconciled, note).
    """
    prev = stmt.opening_balance
    n = len(stmt.transactions)
    failures = 0
    for txn in stmt.transactions:
        if txn.balance is not None and prev is not None:
            expected = prev - (txn.withdrawal or 0.0) + (txn.deposit or 0.0)
            tol = max(tol_abs, tol_rel * abs(expected))
            txn.math_ok = abs(txn.balance - expected) <= tol
            if txn.math_ok is False:
                failures += 1
        else:
            txn.math_ok = None
        if txn.balance is not None:
            prev = txn.balance

    reconciled = failures == 0
    note = (
        f"all {n} rows reconciled"
        if reconciled
        else f"{failures}/{n} rows fail running-balance check"
    )
    stmt.reconciled = reconciled
    stmt.reconcile_note = note
    return reconciled, note


def to_bank_statements(
    ex: ExtractedBankStatement,
    *,
    mode_used: Optional[str] = None,
    source_file_id: Optional[str] = None,
) -> list[BankStatement]:
    """One BankStatement per ExtractedAccount; each is reconciled before returning."""
    out: list[BankStatement] = []
    for acct in ex.accounts:
        stmt = BankStatement(
            bank_name=acct.bank_name or "",
            account_number=acct.account_number,
            currency=acct.currency or "",
            statement_period=acct.statement_period,
            opening_balance=acct.opening_balance,
            closing_balance=acct.closing_balance,
            transactions=[
                BankTransaction(
                    date=_parse_date(t.date),
                    description=t.description or "",
                    bank_ref=t.bank_ref,
                    withdrawal=t.withdrawal,
                    deposit=t.deposit,
                    balance=t.balance,
                )
                for t in acct.transactions
            ],
            source_file_id=source_file_id,
            extract_mode=mode_used,
        )
        reconcile_running_balance(stmt)
        out.append(stmt)
    return out
