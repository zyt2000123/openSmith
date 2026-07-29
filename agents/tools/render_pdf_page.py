"""Render one PDF page to a temporary PNG using Poppler."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

TOOL_META = {
    "name": "render_pdf_page",
    "description": (
        "Render one 1-based PDF page to a temporary PNG for visual inspection. "
        "Requires the Poppler pdftoppm executable to be available on PATH."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the PDF file",
            },
            "page": {
                "type": "integer",
                "description": "1-based page number",
                "default": 1,
            },
            "dpi": {
                "type": "integer",
                "description": "Render resolution from 72 to 300 DPI",
                "default": 144,
            },
        },
        "required": ["path"],
    },
    "path_args": ["path"],
    "permission_level": "read",
    "approval_policy": "never",
    "side_effect": "none",
    "execution_environment": "host",
}

MAX_RENDER_BYTES = 25 * 1024 * 1024


def _find_pdftoppm() -> str | None:
    configured = os.environ.get("SMITH_PDFTOPPM", "").strip()
    if configured and os.path.isfile(configured) and os.access(configured, os.X_OK):
        return configured
    return shutil.which("pdftoppm")


def _execute_sync(*, path: str, page: int = 1, dpi: int = 144) -> str:
    resolved = os.path.realpath(path)
    if not os.path.isfile(resolved):
        return f"Error: PDF file not found: {resolved}"
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        return "Error: page must be a positive 1-based integer"
    if isinstance(dpi, bool) or not isinstance(dpi, int):
        dpi = 144
    dpi = min(max(dpi, 72), 300)

    executable = _find_pdftoppm()
    if executable is None:
        return (
            "Error: Poppler pdftoppm is not available. Install Poppler or set "
            "SMITH_PDFTOPPM to the pdftoppm executable."
        )

    output_dir = Path(tempfile.mkdtemp(prefix="smith-pdf-"))
    prefix = output_dir / "page"
    command = [
        executable,
        "-png",
        "-singlefile",
        "-f",
        str(page),
        "-l",
        str(page),
        "-r",
        str(dpi),
        resolved,
        str(prefix),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "Error: PDF page rendering timed out after 30 seconds"
    except OSError as exc:
        return f"Error: unable to start Poppler: {exc}"

    output_path = prefix.with_suffix(".png")
    if completed.returncode != 0 or not output_path.is_file():
        detail = (completed.stderr or completed.stdout or "unknown Poppler error").strip()
        return f"Error: could not render PDF page {page}: {detail[:500]}"
    try:
        size = output_path.stat().st_size
    except OSError as exc:
        return f"Error: rendered image cannot be inspected: {exc}"
    if size > MAX_RENDER_BYTES:
        output_path.unlink(missing_ok=True)
        return "Error: rendered page exceeds the 25 MB safety limit"

    return f"Rendered PDF page {page} at {dpi} DPI: {output_path} ({size} bytes)"


async def execute(*, path: str, page: int = 1, dpi: int = 144) -> str:
    return await asyncio.to_thread(
        _execute_sync,
        path=path,
        page=page,
        dpi=dpi,
    )
