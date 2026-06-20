#!/usr/bin/env python3
"""Debug repro: Telco Provider A telco extract → normalize → struggle signals."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from accounting_agents import nodes
from accounting_agents.nodes import detect_struggle
from tests.test_nodes import FakeContext

PDF = Path(
    "~/Desktop/LocalTest/TestDoc/GST SR:ZR/"
    "TELCO-BILL-001-sample.pdf"
)


async def main() -> None:
    if not PDF.exists():
        print(f"PDF not found: {PDF}", file=sys.stderr)
        sys.exit(1)
    data = PDF.read_bytes()
    ctx = FakeContext(
        {
            nodes.ARTIFACT_NAME_KEY: "inbox/debug.pdf",
            "channel_id": "C0123456789",
            "file_id": "Fdebug",
            "tax_registered": False,
            "base_currency": "SGD",
            nodes.DIRECTION_KEY: "unknown",
            nodes.DOC_TYPE_KEY: "invoice",
            nodes.CLASSIFY_CONFIDENCE_KEY: 0.9,
        }
    )

    async def load(_ctx):
        return data, "application/pdf"

    nodes._load_pdf_bytes = load
    await nodes.extract_invoice_document_node(ctx)
    tripped, reasons = detect_struggle(ctx.state)
    print("tripped:", tripped)
    for r in reasons:
        print(" ", r)


if __name__ == "__main__":
    asyncio.run(main())
