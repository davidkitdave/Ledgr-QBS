"""Hybrid digital + vision bank statement extraction for ledgr_agent."""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import IO, Any, Optional

from google.genai import types

from ledgr_agent.normalize.bank_statement import normalize_bank_statement
from ledgr_agent.shared.gemini_call_config import default_llm_config
from ledgr_agent.shared.genai_client import lite_model, make_client, std_model
from ledgr_agent.shared.mime import mime_for
from ledgr_agent.models.bank_statement import ReadBankStatement

BANK_READER_INSTRUCTION = """You are extracting a bank / account statement for a Singapore/Malaysia bookkeeping ledger.

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
  bank_name + account_number + currency (format: 'Bank - XXXX - CCY'),
  so duplicating the digits here causes multi-currency sections to collapse
  into a single Excel tab.
- currency as ISO (default SGD). statement_period as printed. account_number
  as printed on the statement, including any dashes (e.g. '072-955554-5').

Do not invent values; leave a field null if it is not visible."""


def _digital_text(
    path: str | Path | IO[bytes] | None = None,
    *,
    pdf_bytes: bytes | None = None,
) -> str:
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


def _is_digital(
    path: str | Path | None = None,
    *,
    pdf_bytes: bytes | None = None,
) -> bool:
    try:
        text = _digital_text(path, pdf_bytes=pdf_bytes)
    except Exception:
        return False
    stripped = text.strip()
    if len(stripped) < 200:
        return False
    return sum(c.isdigit() for c in stripped) >= 5


def _extract_digital(text: str, *, model: str | None = None) -> ReadBankStatement:
    client = make_client()
    resp = client.models.generate_content(
        model=model or lite_model(),
        contents=[text, BANK_READER_INSTRUCTION],
        config=default_llm_config(
            temperature=0,
            response_mime_type="application/json",
            response_schema=ReadBankStatement,
        ),
    )
    return ReadBankStatement.model_validate_json(resp.text or "{}")


def _extract_vision(
    data: bytes,
    mime_type: str,
    *,
    model: str | None = None,
) -> ReadBankStatement:
    client = make_client()
    part = types.Part.from_bytes(data=data, mime_type=mime_type)
    resp = client.models.generate_content(
        model=model or std_model(),
        contents=[part, BANK_READER_INSTRUCTION],
        config=default_llm_config(
            temperature=0,
            response_mime_type="application/json",
            response_schema=ReadBankStatement,
        ),
    )
    return ReadBankStatement.model_validate_json(resp.text or "{}")


def _has_data(statement: ReadBankStatement) -> bool:
    return bool(statement.accounts) and any(a.transactions for a in statement.accounts)


def extract_bank_statement(
    data: bytes,
    mime_type: str,
    *,
    path: str | Path | None = None,
    mode: str = "auto",
    model: str | None = None,
    digital_model: str | None = None,
    vision_model: str | None = None,
) -> tuple[ReadBankStatement, str]:
    """Extract a bank statement; returns (parsed, mode_used).

    mode:
    - ``auto`` — digital via pdfplumber when possible, else vision
    - ``digital`` — force text path (requires ``path`` or PDF ``data``)
    - ``vision`` — force multimodal bytes path
    """
    dig_model = digital_model or model
    vis_model = vision_model or model

    if mode == "vision":
        return _extract_vision(data, mime_type, model=vis_model), "vision"

    if mode == "digital":
        if path is not None:
            text = _digital_text(path)
        elif mime_type == "application/pdf":
            text = _digital_text(pdf_bytes=data)
        else:
            raise ValueError("mode='digital' requires a PDF path or PDF bytes")
        return _extract_digital(text, model=dig_model), "digital"

    if path is not None:
        is_dig = _is_digital(path)
        text_src_kw: dict = {"path": path}
    elif mime_type == "application/pdf":
        is_dig = _is_digital(pdf_bytes=data)
        text_src_kw = {"pdf_bytes": data}
    else:
        is_dig = False
        text_src_kw = {}

    if is_dig:
        text = _digital_text(**text_src_kw).strip()
        if text:
            parsed = _extract_digital(text, model=dig_model)
            if _has_data(parsed):
                return parsed, "digital"

    return _extract_vision(data, mime_type, model=vis_model), "vision"


def read_bank(path: str | Path) -> dict[str, Any]:
    """Read one bank statement file with ONE Gemini call.

    Returns full normalized payload with ``accounts``, ``accounts_normalized``,
    and ``extraction_meta``. On failure ``{status: "error", message: ...}``.
    """
    doc_path = Path(path)
    try:
        data = doc_path.read_bytes()
    except OSError as exc:
        return {"status": "error", "message": f"Could not read {doc_path}: {exc}"}

    mime = mime_for(doc_path)
    t0 = time.perf_counter()
    try:
        parsed, mode_used = extract_bank_statement(
            data,
            mime,
            path=doc_path if mime == "application/pdf" else None,
            mode="auto",
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

    elapsed = round(time.perf_counter() - t0, 2)
    payload = parsed.model_dump()
    accounts_normalized = normalize_bank_statement(payload, extract_mode=mode_used)
    return {
        **payload,
        "accounts_normalized": accounts_normalized,
        "extraction_meta": {
            "gemini_call_count": 1,
            "extract_mode": mode_used,
            "elapsed_seconds": elapsed,
            "bytes_sent": len(data),
            "source_path": str(doc_path),
        },
    }
