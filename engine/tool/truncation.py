"""Compatibility wrapper for callers that only need a tool output preview."""
from __future__ import annotations

from pathlib import Path

from .output_store import MAX_BYTES, MAX_LINES, ToolOutputStore

# Retained for integrations that explicitly override the archive destination.
# The default is resolved at capture time, never cached across runtime resets.
_OUTPUT_DIR: Path | None = None


def truncate_output(text: str, tool_name: str = "") -> str:
    return ToolOutputStore(_OUTPUT_DIR, max_bytes=MAX_BYTES, max_lines=MAX_LINES).capture(
        text, tool_name,
    ).preview
