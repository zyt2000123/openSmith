"""Capture oversized tool output without conflating content and artifact identity."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

MAX_LINES = 2000
MAX_BYTES = 50 * 1024


@dataclass(frozen=True)
class CapturedOutput:
    preview: str
    path: Path | None
    raw_digest: str
    stored_digest: str | None
    size_bytes: int
    truncated: bool

    def metadata(self) -> dict[str, object]:
        return {
            "raw_digest": self.raw_digest,
            "stored_digest": self.stored_digest,
            "size_bytes": self.size_bytes,
            "truncated": self.truncated,
            "path": str(self.path) if self.path is not None else None,
        }


class ToolOutputStore:
    """Archive on demand; resolve the runtime data root at capture time."""

    def __init__(self, root: Path | None = None, *, max_bytes: int = MAX_BYTES,
                 max_lines: int = MAX_LINES) -> None:
        if max_bytes < 1 or max_lines < 1:
            raise ValueError("Output limits must be positive")
        self.root = root
        self.max_bytes = max_bytes
        self.max_lines = max_lines

    def capture(self, text: str, tool_name: str = "") -> CapturedOutput:
        raw = text.encode("utf-8", "replace")
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) <= self.max_bytes and text.count("\n") < self.max_lines:
            return CapturedOutput(text, None, digest, None, len(raw), False)

        # Slice bytes first, then decode on a character boundary. Unlike a
        # whole-line accumulator, even a single huge JSON line has a preview.
        preview = raw[:self.max_bytes].decode("utf-8", "ignore")
        preview = "\n".join(preview.split("\n")[:self.max_lines])
        removed = len(raw) - len(preview.encode("utf-8"))
        path: Path | None = None
        stored_digest: str | None = None
        try:
            from common import config
            from common.paths import _ensure_private_dir
            from engine.safety.approval import _redact_secret_text

            root = self.root if self.root is not None else config.PATHS.data_dir / "tool-output"
            _ensure_private_dir(root)
            safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", tool_name)[:80]
            stored = _redact_secret_text(text).encode("utf-8", "replace")
            # mkstemp uses exclusive creation with mode 0600 from the outset.
            # Never derive artifact identity from wall-clock time or call IDs.
            fd, filename = tempfile.mkstemp(prefix=f"tool_{safe_name}_", suffix=".txt", dir=root)
            path = Path(filename)
            with os.fdopen(fd, "wb") as target:
                target.write(stored)
            stored_digest = hashlib.sha256(stored).hexdigest()
            hint = f"Full output saved to: {path}\nUse read_file with offset/limit to view specific sections."
        except (OSError, RuntimeError):
            if path is not None:
                path.unlink(missing_ok=True)
            path = None
            hint = "(Failed to save full output to file)"

        return CapturedOutput(
            f"{preview}\n\n...{removed} bytes truncated...\n\n{hint}",
            path, digest, stored_digest, len(raw), True,
        )
