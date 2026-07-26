from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from pypdf import PdfWriter


ROOT = Path(__file__).resolve().parents[3]


def _load_tool_module(name: str):
    path = ROOT / "agents" / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_pdf(path: Path, page_count: int = 2) -> None:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    writer.add_metadata({"/Title": "PDF tool test"})
    with path.open("wb") as file:
        writer.write(file)


def _write_text_pdf(path: Path) -> None:
    stream = b"BT /F1 18 Tf 72 720 Td (Hello PDF) Tj ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{number} 0 obj\n".encode())
        body.extend(obj)
        body.extend(b"\nendobj\n")
    xref_offset = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode())
    body.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    path.write_bytes(body)


def test_read_pdf_parses_page_ranges_and_deduplicates_pages() -> None:
    module = _load_tool_module("read_pdf")

    assert module._parse_pages("1-3,2,5", 5) == [0, 1, 2, 4]
    assert module._parse_pages("all", 2) == [0, 1]


def test_read_pdf_rejects_invalid_page_ranges() -> None:
    module = _load_tool_module("read_pdf")

    try:
        module._parse_pages("0-2", 3)
    except ValueError as exc:
        assert "out of range" in str(exc)
    else:
        raise AssertionError("invalid PDF page range was accepted")


def test_read_pdf_returns_page_aware_metadata_and_scanned_hint(tmp_path: Path) -> None:
    module = _load_tool_module("read_pdf")
    pdf_path = tmp_path / "report.pdf"
    _write_pdf(pdf_path)

    result = asyncio.run(module.execute(path=str(pdf_path), pages="2"))

    assert "pages: 2; selected: 2" in result
    assert "title: PDF tool test" in result
    assert "--- page 2 ---" in result
    assert "image-only" in result


def test_read_pdf_extracts_text_from_a_text_pdf(tmp_path: Path) -> None:
    module = _load_tool_module("read_pdf")
    pdf_path = tmp_path / "text.pdf"
    _write_text_pdf(pdf_path)

    result = asyncio.run(module.execute(path=str(pdf_path)))

    assert "Hello PDF" in result


def test_read_pdf_reports_missing_file() -> None:
    module = _load_tool_module("read_pdf")

    result = asyncio.run(module.execute(path="/tmp/does-not-exist-smith.pdf"))

    assert result.startswith("Error: file not found:")


def test_render_pdf_page_validates_page_before_poppler_lookup(tmp_path: Path) -> None:
    module = _load_tool_module("render_pdf_page")
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"not a real PDF")

    result = asyncio.run(module.execute(path=str(pdf_path), page=0))

    assert result == "Error: page must be a positive 1-based integer"
