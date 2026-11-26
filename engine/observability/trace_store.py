"""Bounded, local JSONL trace storage for one Agent run.

Records are hash-chained (``seq``/``prev_hash``/``hash``) so the trace is
tamper-evident: editing, reordering, or deleting a record breaks the chain and
is detected by :meth:`TraceStore.verify`.  :meth:`TraceStore.seal` anchors the
chain head at run end, making a later rollback detectable too.
"""

from __future__ import annotations

import json
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.hash_chain import ChainVerification, HashChainLog
from common.paths import PRIVATE_DIR_MODE
from engine.execution.events import EventType, ExecutionEvent
from engine.safety.redaction import (
    TRACE_SECRET_RE as _SECRET_IN_VALUE,
    redact_trace_secrets as _redact_secrets_in_text,
)

_SENSITIVE_KEY = re.compile(r"(?:token|secret|password|passwd|api[_-]?key|authorization)", re.I)
# Numeric token-metric keys are not secrets and must persist as values, not
# "[REDACTED]" (the name-based redaction would otherwise hit the "token"
# substring).  Keep this in sync with projections.RunSummaryProjection's
# token keys so summary and trace agree about the same run.
_SAFE_METRIC_KEYS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "context_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    # Single-turn TaskBudget metrics.
    "max_total_tokens",
    "spent_tokens",
    "subagent_tokens",
    "estimated_tokens",
    "in_flight_tokens",
    "remaining_tokens",
    "tokens_limit",
    "tokens_used",
}
_MAX_VALUE_CHARS = 4096
_MAX_DEPTH = 4
_DEFERRED_SYNC_EVENTS = {
    EventType.RAW_RESPONSE_EVENT,
    EventType.PROVISIONAL_TEXT_DELTA,
    # Per-iteration progress records.  An fsync per event ran inline on the SSE
    # delivery path, so a tool-heavy turn paid hundreds of them; these
    # carry no resume or approval decision, and ``seal`` fsyncs the whole log
    # before it anchors the head, so a finished run is durable regardless.
    # TOOL_CALL_START/RESULT stay synchronous to match the run-state store,
    # whose approval transition rides on the same events.
    EventType.THINKING,
    EventType.TOKEN_USAGE,
    EventType.CONTEXT_USAGE,
    EventType.BUDGET_UPDATE,
}


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

    _CHAIN_NAMESPACE = "agent-smith-trace"

    def __init__(self, profile_dir: Path) -> None:
        self.root = Path(profile_dir) / "traces"
        self.root.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
        self.root.chmod(PRIVATE_DIR_MODE)
        self._chains: dict[str, HashChainLog] = {}

    def _path(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
            raise ValueError("invalid trace run id")
        return self.root / f"{run_id}.jsonl"

    def _chain(self, run_id: str) -> HashChainLog:
        chain = self._chains.get(run_id)
        if chain is None:
            chain = HashChainLog(
                self._path(run_id),
                namespace=self._CHAIN_NAMESPACE,
            )
            self._chains[run_id] = chain
        return chain

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
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "type": record_type,
            "data": _bounded_trace_value(data),
        }
        # seq/prev_hash/hash are assigned by the chain under its own lock so
        # sequence assignment and the append write stay atomic per record.
        self._chain(run_id).append(record, sync=sync)

    def seal(self, run_id: str) -> dict:
        """Anchor the chain head so a later rollback of this run is detected."""
        return self._chain(run_id).seal()

    def reopen(self, run_id: str) -> None:
        """Drop a stale anchor so a resumed run's chain stays verifiable.

        Called when a recoverable run is resumed: the previous run finished
        and sealed an anchor, and extending the chain without clearing it would
        make every verify() report a false ``anchor mismatch``.
        """
        self._chain(run_id).unseal()

    def verify(self, run_id: str) -> ChainVerification:
        """Walk this run's chain and report the first integrity failure."""
        return self._chain(run_id).verify()

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

    def read_from(
        self,
        run_id: str,
        *,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Read records appended after a previously returned byte offset."""
        path = self._path(run_id)
        if not path.is_file():
            return [], 0
        try:
            size = path.stat().st_size
        except OSError:
            return [], 0
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("trace offset must be a non-negative integer")
        if offset > size:
            offset = 0

        records: list[dict[str, Any]] = []
        with path.open("rb") as handle:
            handle.seek(offset)
            next_offset = offset
            while line := handle.readline():
                line_start = next_offset
                next_offset = handle.tell()
                if not line.endswith(b"\n"):
                    next_offset = line_start
                    break
                try:
                    value = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict):
                    records.append(value)
        return records, next_offset

    def list_run_ids(self) -> list[str]:
        """List trace identifiers without parsing trace payloads."""
        return sorted(
            path.stem
            for path in self.root.glob("*.jsonl")
            if path.stem and path.stem not in {".", ".."}
        )

    def iter_runs(self) -> list[tuple[str, list[dict[str, Any]]]]:
        """Return all valid traces without exposing the on-disk layout."""
        traces: list[tuple[str, list[dict[str, Any]]]] = []
        for path in sorted(self.root.glob("*.jsonl")):
            try:
                traces.append((path.stem, self.read(path.stem)))
            except ValueError:
                continue
        return traces
