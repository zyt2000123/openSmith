"""Render one PDF page to a temporary PNG using Poppler."""

from __future__ import annotations

import asyncio
import hashlib
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
MAX_RENDER_FILES = 32
_SAFE_ENV_KEYS = ("LANG", "LC_ALL", "TERM", "TZ", "NO_COLOR")

_render_dir: Path | None = None


def _safe_environment(render_dir: Path) -> dict[str, str]:
    """Minimal env for the Poppler subprocess, mirroring shell/git_ops.

    pdftoppm inherits the server process environment by default; stripping it
    keeps service credentials out of a subprocess that parses a model-supplied
    PDF.  ``PATH`` stays so any helper the binary spawns can still be found.
    """
    environment = {
        "PATH": os.environ.get("PATH") or os.defpath,
        "HOME": str(render_dir),
    }
    for key in _SAFE_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment


def _tidy_render_dir() -> None:
    """Bound the process-global render dir so a long-lived server does not
    accumulate rendered PNGs forever.

    Re-renders overwrite deterministic file names, so growth tracks distinct
    PDFs rendered; once the cap is exceeded the oldest files are evicted by
    mtime.  Callers consume a returned PNG immediately, and re-rendering the
    same PDF regenerates it, so eviction of old artifacts is safe.
    """
    if _render_dir is None or not _render_dir.is_dir():
        return
    try:
        candidates = sorted(
            (p for p in _render_dir.glob("*.png") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError:
        return
    while len(candidates) > MAX_RENDER_FILES:
        oldest = candidates.pop(0)
        try:
            oldest.unlink(missing_ok=True)
        except OSError:
            pass


def _render_output_dir() -> Path:
    """Return one reusable render directory for this process.

    A fresh ``mkdtemp`` per call left a directory and its PNG behind on every
    render, since the caller still needs the file after ``execute`` returns and
    nothing ever swept them.  Reusing a single ``mkdtemp`` keeps its 0700 mode
    and symlink safety while bounding the footprint; deterministic file names
    below let a repeated render overwrite instead of accumulate.
    """
    global _render_dir
    if _render_dir is None or not _render_dir.is_dir():
        _render_dir = Path(tempfile.mkdtemp(prefix="smith-pdf-"))
    return _render_dir


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

    output_dir = _render_output_dir()
    try:
        info = os.stat(resolved)
    except OSError as exc:
        return f"Error: cannot inspect PDF: {exc}"
    # Identity, not just the path: a PDF edited in place must not be served an
    # image rendered from its previous contents.
    identity = f"{resolved}\0{info.st_size}\0{info.st_mtime_ns}\0{page}\0{dpi}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    prefix = output_dir / digest
    # Clear the target before rendering.  Otherwise a failed render leaves an
    # earlier successful PNG sitting at this exact path, and any caller still
    # holding that path from prior context reads stale content as fresh.
    prefix.with_suffix(".png").unlink(missing_ok=True)
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
            env=_safe_environment(output_dir),
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

    _tidy_render_dir()
    return f"Rendered PDF page {page} at {dpi} DPI: {output_path} ({size} bytes)"


async def execute(*, path: str, page: int = 1, dpi: int = 144) -> str:
    return await asyncio.to_thread(
        _execute_sync,
        path=path,
        page=page,
        dpi=dpi,
    )
