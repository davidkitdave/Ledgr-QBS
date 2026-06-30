"""Tiny PDF builder (stdlib only) for synthetic eval fixtures."""

from __future__ import annotations

import zlib


def _escape_pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _page_stream(lines: list[tuple[float, float, str]]) -> bytes:
    content_parts: list[str] = ["BT", "/F1 11 Tf"]
    for x, y, text in lines:
        content_parts.append(f"1 0 0 1 {x:.1f} {y:.1f} Tm ({_escape_pdf_text(text)}) Tj")
    content_parts.append("ET")
    stream = "\n".join(content_parts).encode("latin-1", errors="replace")
    return zlib.compress(stream)


def make_pdf(lines: list[tuple[float, float, str]], *, title: str = "Document") -> bytes:
    """Build a one-page PDF. Each line is ``(x, y, text)`` in points from bottom-left."""
    return make_multipage_pdf([lines], title=title)


def make_multipage_pdf(
    pages: list[list[tuple[float, float, str]]],
    *,
    title: str = "Document",
) -> bytes:
    """Build a multi-page PDF. Each page is a list of ``(x, y, text)`` tuples."""
    if not pages:
        pages = [[]]

    page_count = len(pages)
    # Object IDs: 1=catalog, 2=pages, 3..2+N=page objs, 3+N..2+2N=content streams, last=font
    font_id = 3 + page_count * 2
    page_ids = list(range(3, 3 + page_count))
    content_ids = list(range(3 + page_count, 3 + page_count * 2))

    objects: list[bytes] = []
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(f"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n".encode())
    objects.append(
        f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {page_count} >>\nendobj\n".encode()
    )

    page_objects: list[bytes] = []
    content_objects: list[bytes] = []
    for page_idx, page_lines in enumerate(pages):
        page_id = page_ids[page_idx]
        content_id = content_ids[page_idx]
        compressed = _page_stream(page_lines)
        page_objects.append(
            (
                f"{page_id} 0 obj\n<< /Type /Page /Parent 2 0 R "
                f"/MediaBox [0 0 612 792] /Contents {content_id} 0 R "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>\nendobj\n"
            ).encode()
        )
        content_objects.append(
            f"{content_id} 0 obj\n<< /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n".encode()
            + compressed
            + b"\nendstream\nendobj\n"
        )

    objects.extend(page_objects)
    objects.extend(content_objects)

    objects.append(
        f"{font_id} 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n".encode()
    )

    header = f"%PDF-1.4\n%{title}\n".encode("latin-1", errors="replace")
    body = b""
    offsets = [0]
    for obj in objects:
        offsets.append(len(header) + len(body))
        body += obj

    xref_offset = len(header) + len(body)
    xref = ["xref", f"0 {len(objects) + 1}", "0000000000 65535 f "]
    for off in offsets[1:]:
        xref.append(f"{off:010d} 00000 n ")
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    )
    return header + body + "\n".join(xref).encode() + b"\n" + trailer.encode()
