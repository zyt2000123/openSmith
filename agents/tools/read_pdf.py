"""PDF reader tool provider with bounded, page-aware text extraction."""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Iterable

TOOL_META = {
    "name": "read_pdf",
    "description": (
        "Read text and metadata from a local PDF. Supports 1-based page ranges "
        "such as '1-3,7'; use render_pdf_page when visual layout matters."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the PDF file",
            },
            "pages": {
                "type": "string",
                "description": "1-based pages, for example '1-3,7'; defaults to all pages",
                "default": "",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum extracted characters returned",
                "default": 30000,
            },
            "password": {
                "type": "string",
                "description": "Optional password for an encrypted PDF",
            },
        },
        "required": ["path"],
    },
    "path_args": ["path"],
    "permission_level": "read",
    "approval_policy": "never",
    "side_effect": "none",
    "execution_environment": "host",
    "timeout_seconds": 120,
}

MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_PAGES_PER_CALL = 100
DEFAULT_MAX_CHARS = 30_000
MAX_CHARS = 100_000
_PAGE_PART = re.compile(r"^(\d+)(?:-(\d+))?$")


def _parse_pages(spec: str, page_count: int) -> list[int]:
    """Parse a 1-based page selection into zero-based page indexes.

    An empty/``all`` selection reads a bounded window of the whole document
    (capped at ``MAX_PAGES_PER_CALL``) rather than erroring on a long PDF;
    an *explicit* selection larger than the cap is a user error and fails
    loudly here.
    """
    normalized = spec.strip().lower()
    if not normalized or normalized == "all":
        return list(range(min(page_count, MAX_PAGES_PER_CALL)))

    indexes: list[int] = []
    seen: set[int] = set()
    for raw_part in normalized.split(","):
        part = raw_part.strip()
        match = _PAGE_PART.fullmatch(part)
        if match is None:
            raise ValueError(f"Invalid page selection: {raw_part.strip()!r}")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start or end > page_count:
            raise ValueError(f"Page selection out of range: {part!r} (PDF has {page_count} pages)")
        for page in range(start, end + 1):
            index = page - 1
            if index not in seen:
                indexes.append(index)
                seen.add(index)

    if len(indexes) > MAX_PAGES_PER_CALL:
        raise ValueError(
            f"Too many pages requested ({len(indexes)}); limit the selection to {MAX_PAGES_PER_CALL} pages"
        )
    return indexes


def _format_page_selection(indexes: Iterable[int]) -> str:
    return ",".join(str(index + 1) for index in indexes)


def _metadata_lines(metadata: object) -> list[str]:
    if metadata is None:
        return []
    fields = (
        ("title", "/Title"),
        ("author", "/Author"),
        ("subject", "/Subject"),
        ("creator", "/Creator"),
    )
    lines: list[str] = []
    for label, key in fields:
        try:
            value = metadata.get(key)
        except AttributeError:
            value = None
        if value:
            lines.append(f"{label}: {str(value).strip()[:300]}")
    return lines


_READ_TIMEOUT_BUDGET = 100.0  # seconds; slightly under TOOL_META timeout_seconds so a
                              # clean error surfaces before the registry cancels the await


class _PdfTimeout(Exception):
    """Internal signal that the per-call extraction budget was exceeded."""


def _extract_with_pypdf(
    reader: object, indexes: Iterable[int], deadline: float | None = None
) -> dict[int, str]:
    pages = getattr(reader, "pages")
    result: dict[int, str] = {}
    for index in indexes:
        if deadline is not None and time.monotonic() > deadline:
            raise _PdfTimeout()
        try:
            result[index] = pages[index].extract_text() or ""
        except Exception as exc:
            result[index] = f"[text extraction failed: {type(exc).__name__}]"
    return result


def _extract_with_pdfplumber(
    path: str,
    indexes: Iterable[int],
    password: str | None,
    deadline: float | None = None,
) -> dict[int, str]:
    import pdfplumber

    result: dict[int, str] = {}
    with pdfplumber.open(path, password=password) as pdf:
        for index in indexes:
            if deadline is not None and time.monotonic() > deadline:
                raise _PdfTimeout()
            try:
                result[index] = pdf.pages[index].extract_text() or ""
            except Exception as exc:
                result[index] = f"[text extraction failed: {type(exc).__name__}]"
    return result


def _execute_sync(
    *,
    path: str,
    pages: str = "",
    max_chars: int = DEFAULT_MAX_CHARS,
    password: str | None = None,
) -> str:
    resolved = os.path.realpath(path)
    if not os.path.exists(resolved):
        return f"Error: file not found: {resolved}"
    if not os.path.isfile(resolved):
        return f"Error: not a regular file: {resolved}"

    try:
        if os.path.getsize(resolved) > MAX_PDF_BYTES:
            return f"Error: PDF exceeds the {MAX_PDF_BYTES // (1024 * 1024)} MB safety limit: {resolved}"
    except OSError as exc:
        return f"Error: cannot inspect PDF: {exc}"

    try:
        from pypdf import PdfReader
    except ImportError:
        return "Error: PDF support is unavailable because pypdf is not installed."

    try:
        reader = PdfReader(resolved, strict=False)
        if reader.is_encrypted:
            if not password:
                return "Error: PDF is encrypted; provide the password to read it."
            if not reader.decrypt(password):
                return "Error: PDF password is incorrect or unsupported."
        page_count = len(reader.pages)
        spec = pages.strip().lower()
        indexes = _parse_pages(pages, page_count)
        all_truncated = (not spec or spec == "all") and page_count > len(indexes)
    except ValueError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error reading PDF {resolved}: {type(exc).__name__}: {exc}"

    # pdfplumber wins whenever it is available, so try it first: extracting with
    # pypdf up front only to discard the whole result parsed every page twice.
    # A deadline bounds the extraction phase from inside the worker thread: the
    # registry's wait_for can cancel the await but cannot stop the thread.
    deadline = time.monotonic() + _READ_TIMEOUT_BUDGET
    try:
        text_by_page = _extract_with_pdfplumber(resolved, indexes, password, deadline)
        extractor = "pdfplumber"
    except _PdfTimeout:
        return f"Error: PDF reading timed out after {_READ_TIMEOUT_BUDGET:g}s"
    except Exception:
        text_by_page = {}
        extractor = "pypdf"
    if not text_by_page:
        try:
            text_by_page = _extract_with_pypdf(reader, indexes, deadline)
            extractor = "pypdf"
        except _PdfTimeout:
            return f"Error: PDF reading timed out after {_READ_TIMEOUT_BUDGET:g}s"

    # An empty page is legitimate — a scanned PDF has no text layer — but a page
    # whose extraction *raised* is a failure.  When every requested page failed on
    # both backends the old code still returned a success-shaped document whose
    # only content was the per-page failure placeholders.
    failed = sum(
        1 for index in indexes
        if text_by_page.get(index, "").startswith("[text extraction failed")
    )
    if indexes and failed == len(indexes):
        return (
            f"Error: text extraction failed for every requested page of {resolved} "
            f"({failed} page(s)); the file may be corrupt or need a password"
        )

    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        max_chars = DEFAULT_MAX_CHARS
    max_chars = min(max(max_chars, 1_000), MAX_CHARS)
    selection_note = (
        f" (first {MAX_PAGES_PER_CALL} of {page_count} shown; specify pages= to read more)"
        if all_truncated else ""
    )
    output = [
        f"# {resolved}",
        f"pages: {page_count}; selected: {_format_page_selection(indexes)}{selection_note}; extractor: {extractor}",
    ]
    output.extend(_metadata_lines(reader.metadata))

    current_length = sum(len(line) + 1 for line in output)
    for index in indexes:
        page_text = text_by_page.get(index, "").strip()
        if not page_text:
            page_text = "[No extractable text on this page; it may be scanned or image-only.]"
        block = f"\n--- page {index + 1} ---\n{page_text}\n"
        remaining = max_chars - current_length
        if len(block) > remaining:
            if remaining > 80:
                output.append(block[:remaining].rstrip())
            # The marker belongs on every truncated path.  Emitting it only when
            # a partial page still fit let a tight budget drop pages silently,
            # leaving the reader no way to tell the output was cut short.
            output.append("\n...[PDF output truncated; request a smaller page range or a larger max_chars value]")
            break
        output.append(block)
        current_length += len(block)

    return "\n".join(output)


async def execute(
    *,
    path: str,
    pages: str = "",
    max_chars: int = DEFAULT_MAX_CHARS,
    password: str | None = None,
) -> str:
    return await asyncio.to_thread(
        _execute_sync,
        path=path,
        pages=pages,
        max_chars=max_chars,
        password=password,
    )
