"""pypdf #24–#31 (eight GHSA advisories, all DoS) — real-path controls.

The advisories are all resource-exhaustion during *parsing*: huge font data,
ToUnicode streams, embedded files, page-label math, malformed FlateDecode.
The installed pin (6.19.0) carries every upstream fix — that's the version
verdict. These probes pin the *application* boundary on the real ingestion
path (`knowledge.loader.extract_text_from_pdf`, reached by /documents
uploads, intake uploads, email attachments and external-source PDFs):

  * extracted output is bounded (pages and chars) — a legitimate document
    passes whole, a padded one is truncated, neither grows without bound;
  * malformed input refuses (exception) instead of hanging or 500-ing
    silently;
  * the caps exist and are sane — a regression that deletes the bounds
    fails CI even if the library pin stays patched.
"""
from __future__ import annotations

import pytest
from pypdf.errors import PyPdfError

from openexecutive.knowledge import loader


def _make_pdf(page_texts: list[str]) -> bytes:
    """Smallest valid PDF: one text object per page, no compression."""
    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    font_obj = 3 + 2 * len(page_texts)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(page_texts)))
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_texts)} >>".encode()
    )
    for i, text in enumerate(page_texts):
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_obj} 0 R >> >> "
                f"/Contents {4 + 2 * i} 0 R >>"
            ).encode()
        )
        stream = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode()
        objects.append(
            b"<< /Length " + str(len(stream)).encode()
            + b" >>\nstream\n" + stream + b"\nendstream"
        )
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    )

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF"
    ).encode()
    return bytes(out)


def test_pdf_extracts_all_pages_when_under_cap(tmp_path) -> None:
    pdf = tmp_path / "real.pdf"
    pdf.write_bytes(_make_pdf(["alpha one", "beta two", "gamma three"]))
    text = loader.extract_text_from_pdf(pdf)
    assert "alpha one" in text and "beta two" in text
    assert "gamma three" in text


def test_pdf_page_cap_truncates(tmp_path) -> None:
    pdf = tmp_path / "many.pdf"
    pdf.write_bytes(_make_pdf([f"page number {i} content" for i in range(5)]))
    text = loader.extract_text_from_pdf(pdf, max_pages=2)
    assert "page number 0" in text and "page number 1" in text
    assert "page number 2" not in text and "page number 4" not in text


def test_pdf_char_cap_bounds_output(tmp_path) -> None:
    pdf = tmp_path / "padded.pdf"
    pdf.write_bytes(_make_pdf(["x" * 900, "y" * 900, "z" * 900]))
    text = loader.extract_text_from_pdf(pdf, max_chars=1000)
    assert len(text) < 2000
    assert "xxx" in text


def test_pdf_malformed_input_refuses(tmp_path) -> None:
    """A truncated/garbage file raises promptly — the DoS advisories die in
    the parser, so 'refuse' is the safe outcome, not hang or silent empty."""
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 truncated garbage with no objects")
    with pytest.raises(PyPdfError):
        loader.extract_text_from_pdf(bad)


def test_pdf_default_bounds_exist() -> None:
    assert 0 < loader.PDF_MAX_PAGES <= 10_000
    assert 0 < loader.PDF_MAX_CHARS <= 10_000_000
