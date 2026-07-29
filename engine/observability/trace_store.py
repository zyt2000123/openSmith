"""Bounded, local JSONL trace storage for one Agent run."""

from __future__ import annotations

import json
import os
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.paths import PRIVATE_DIR_MODE, PRIVATE_FILE_MODE

from engine.execution.events import EventType, ExecutionEvent


_SENSITIVE_KEY = re.compile(r"(?:token|secret|password|passwd|api[_-]?key|authorization)", re.I)
_SAFE_METRIC_KEYS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "context_tokens",
}
_MAX_VALUE_CHARS = 4096
_MAX_DEPTH = 4
_DEFERRED_SYNC_EVENTS = {
    EventType.RAW_RESPONSE_EVENT,
    EventType.PROVISIONAL_TEXT_DELTA,
}
# Name-based redaction cannot see a credential carried *inside* a value, and
# TOOL_CALL_START writes the full tool arguments — so a shell command holding a
# bearer token landed here verbatim, under the innocuous key "command".
# The leading (?<![A-Za-z0-9]) is essential, not decoration: "sk-" occurs inside
# ordinary kebab-case words — ta*sk-*scheduler, di*sk-*usage, ri*sk-*scoring —
# and without a boundary the pattern ate whole filenames.  The auth-header branch
# likewise needs a digit, or "Basic authentication" matched as a credential.
_SECRET_IN_VALUE = re.compile(
    r"""(?xi)
    (?<![A-Za-z0-9])
    (?: \b(?:bearer|basic)\s+(?=[A-Za-z0-9._~+/=-]*[0-9])
          [A-Za-z0-9._~+/=-]{16,}                                     # auth headers
      | sk-[A-Za-z0-9_-]{16,}                                         # OpenAI/Anthropic style
      | gh[pousr]_[A-Za-z0-9]{20,}                                    # GitHub
      | AKIA[0-9A-Z]{12,}                                             # AWS key id
      | xox[abprs]-[A-Za-z0-9-]{10,}                                  # Slack
      | eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+     # JWT
    )
    """
)


def _redact_secrets_in_text(text: str) -> str:
    return _SECRET_IN_VALUE.sub("[REDACTED]", text)


def _bounded_trace_value(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            result[key_text] = (
                "[REDACTED]"
                if _SENSITIVE_KEY.search(key_text) and key_text.lower() not in _SAFE_METRIC_KEYS
                else _bounded_trace_value(item, depth + 1)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [_bounded_trace_value(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return _redact_secrets_in_text(value[:_MAX_VALUE_CHARS])
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _redact_secrets_in_text(str(value)[:_MAX_VALUE_CHARS])


class TraceStore:
    """Append and read bounded execution events without blocking a run."""

    def __init__(self, profile_dir: Path) -> None:
        self.root = Path(profile_dir) / "traces"
        self.root.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
        self.root.chmod(PRIVATE_DIR_MODE)
        self._next_seq: dict[str, int] = {}

    def _path(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
            raise ValueError("invalid trace run id")
        return self.root / f"{run_id}.jsonl"

    def _sequence(self, run_id: str, path: Path) -> int:
        if run_id not in self._next_seq:
            current = 0
            if path.is_file():
                with path.open(encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            current = max(
                                current,
                                int(json.loads(line).get("seq", 0)),
                            )
                        except (ValueError, TypeError, json.JSONDecodeError):
                            continue
            self._next_seq[run_id] = current
        self._next_seq[run_id] += 1
        return self._next_seq[run_id]

    def append(self, run_id: str, event: ExecutionEvent) -> None:
        self._append_record(
            run_id,
            event.type.value,
            event.data,
            sync=event.type not in _DEFERRED_SYNC_EVENTS,
        )

    def append_prompt_manifest(self, run_id: str, manifest: dict[str, Any]) -> None:
        """Persist a redacted prompt-provenance receipt without prompt text."""
        self._append_record(run_id, "prompt_manifest", manifest)

    def _append_record(
        self,
        run_id: str,
        record_type: str,
        data: dict[str, Any],
        *,
        sync: bool = True,
    ) -> None:
        path = self._path(run_id)
        record = {
            "seq": self._sequence(run_id, path),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "type": record_type,
            "data": _bounded_trace_value(data),
        }
        payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, PRIVATE_FILE_MODE)
        try:
            os.write(fd, payload)
            if sync:
                os.fsync(fd)
        finally:
            os.close(fd)
        path.chmod(PRIVATE_FILE_MODE)

    def read(self, run_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        path = self._path(run_id)
        if not path.is_file() or (limit is not None and limit < 1):
            return []
        records: list[dict[str, Any]] | deque[dict[str, Any]]
        records = [] if limit is None else deque(maxlen=limit)
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    records.append(value)
        return list(records)

    def iter_runs(self) -> list[tuple[str, list[dict[str, Any]]]]:
        """Return all valid traces without exposing the on-disk layout."""
        traces: list[tuple[str, list[dict[str, Any]]]] = []
        for path in sorted(self.root.glob("*.jsonl")):
            try:
                traces.append((path.stem, self.read(path.stem)))
            except ValueError:
                continue
        return traces
