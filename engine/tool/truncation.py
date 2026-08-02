"""Tool output truncation — save full output to file, return preview."""

from __future__ import annotations

import os
import time
from pathlib import Path

MAX_LINES = 2000
MAX_BYTES = 50 * 1024  # 50KB

_OUTPUT_DIR: Path | None = None


def _get_output_dir() -> Path:
    global _OUTPUT_DIR
    if _OUTPUT_DIR is None:
        try:
            from common.config import DATA_DIR
            _OUTPUT_DIR = DATA_DIR / "tool-output"
        except Exception:
            _OUTPUT_DIR = Path.home() / ".agent-smith" / "tool-output"
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return _OUTPUT_DIR


def truncate_output(text: str, tool_name: str = "") -> str:
    """If text exceeds MAX_LINES or MAX_BYTES, truncate and save full content to file.

    Returns truncated text with a hint pointing to the full output file.
    If text is within limits, returns it unchanged.
    """
    lines = text.split("\n")
    total_bytes = len(text.encode("utf-8"))

    if len(lines) <= MAX_LINES and total_bytes <= MAX_BYTES:
        return text

    out: list[str] = []
    byte_count = 0
    hit_bytes = False

    for i, line in enumerate(lines):
        if len(out) >= MAX_LINES:
            break
        line_bytes = len(line.encode("utf-8")) + (1 if i > 0 else 0)
        if byte_count + line_bytes > MAX_BYTES:
            hit_bytes = True
            break
        out.append(line)
        byte_count += line_bytes

    removed = total_bytes - byte_count if hit_bytes else len(lines) - len(out)
    unit = "bytes" if hit_bytes else "lines"
    preview = "\n".join(out)

    output_dir = _get_output_dir()
    filename = f"tool_{tool_name}_{int(time.time())}_{os.getpid()}.txt"
    filepath = output_dir / filename

    try:
        # The full output file is a persistent disk sink the model may later
        # read back via read_file, so it must carry the same embedded-secret
        # redaction as the audit trail — a grep/read of a credential file
        # must not round-trip the token onto disk verbatim.
        from engine.safety.approval import _redact_secret_text

        filepath.write_text(_redact_secret_text(text), encoding="utf-8")
        filepath.chmod(0o600)
        hint = f"Full output saved to: {filepath}\nUse read_file with offset/limit to view specific sections."
    except Exception:
        hint = "(Failed to save full output to file)"

    return f"{preview}\n\n...{removed} {unit} truncated...\n\n{hint}"
