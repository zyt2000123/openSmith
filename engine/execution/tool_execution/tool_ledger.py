"""Persistent idempotency ledger for side-effecting tool calls."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from common.paths import PRIVATE_DIR_MODE, PRIVATE_FILE_MODE
from engine.tool.interface import ToolResult


@dataclass(frozen=True)
class ToolLedgerDecision:
    claimed: bool
    result: ToolResult | None = None


class ToolExecutionLedger:
    """Cross-process-safe ledger keyed by one run and one tool idempotency key.

    A completed side effect is replayed from its recorded result. A call left
    running or failing is treated as uncertain, so a retry cannot silently
    duplicate an external write.
    """

    def __init__(
        self,
        profile_dir: Path,
        run_id: str,
        *,
        replay_existing: bool = False,
    ) -> None:
        self.run_id = run_id
        self.replay_existing = replay_existing
        self._call_keys: dict[str, str] = {}
        self._semantic_occurrences: dict[str, int] = {}
        self.path = Path(profile_dir) / "runs" / "tool_executions.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
        self.path.parent.chmod(PRIVATE_DIR_MODE)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS tool_executions (
                    run_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    is_error INTEGER NOT NULL DEFAULT 0,
                    error_kind TEXT,
                    side_effect_status TEXT NOT NULL DEFAULT 'unknown',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, idempotency_key)
                )"""
            )
            connection.commit()
        finally:
            connection.close()
        self.path.chmod(PRIVATE_FILE_MODE)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _semantic_key(tool_name: str, arguments: dict) -> str:
        payload = json.dumps(
            {"tool": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def idempotency_key_for(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: dict,
    ) -> str:
        """Return a stable key for one logical side effect.

        Provider call IDs are recreated after a resumed model invocation.  The
        occurrence index preserves intentionally repeated equivalent calls
        while letting a resumed run replay the matching earlier operation.
        """
        existing = self._call_keys.get(call_id)
        if existing is not None:
            return existing

        semantic = self._semantic_key(tool_name, arguments)
        prior = self._semantic_occurrences.get(semantic, 0)
        if not self.replay_existing:
            prior = max(prior, self._persisted_occurrences(semantic))
        occurrence = prior + 1
        self._semantic_occurrences[semantic] = occurrence
        key = f"semantic:{semantic}:{occurrence}"
        self._call_keys[call_id] = key
        return key

    def _persisted_occurrences(self, semantic: str) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM tool_executions "
                "WHERE run_id=? AND idempotency_key LIKE ?",
                (self.run_id, f"semantic:{semantic}:%"),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            connection.close()

    def begin(self, *, call_id: str, tool_name: str, idempotency_key: str) -> ToolLedgerDecision:
        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, content, is_error, error_kind, side_effect_status "
                "FROM tool_executions WHERE run_id=? AND idempotency_key=?",
                (self.run_id, idempotency_key),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO tool_executions "
                    "(run_id, idempotency_key, tool_name, call_id, status, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (self.run_id, idempotency_key, tool_name, call_id, "running", now, now),
                )
                connection.commit()
                return ToolLedgerDecision(claimed=True)

            status, content, is_error, error_kind, side_effect_status = row
            connection.commit()
            if status == "completed":
                return ToolLedgerDecision(
                    claimed=False,
                    result=ToolResult(
                        call_id=call_id,
                        content=str(content),
                        is_error=bool(is_error),
                        error_kind=error_kind,
                        side_effect_status="completed",
                        metadata={"replayed": True},
                    ),
                )
            return ToolLedgerDecision(
                claimed=False,
                result=ToolResult(
                    call_id=call_id,
                    content=(
                        "Side effect status is uncertain; refusing to execute the "
                        f"tool again: {tool_name}"
                    ),
                    is_error=True,
                    error_kind="side_effect_uncertain",
                    retryable=False,
                    side_effect_status="unknown",
                    metadata={"ledger_status": status, "previous_error_kind": error_kind},
                ),
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def finish(
        self,
        *,
        call_id: str,
        idempotency_key: str,
        result: ToolResult,
    ) -> None:
        successful = not result.is_error
        now = self._now()
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE tool_executions SET status=?, call_id=?, content=?, is_error=?, "
                "error_kind=?, side_effect_status=?, updated_at=? "
                "WHERE run_id=? AND idempotency_key=?",
                (
                    "completed" if successful else "uncertain",
                    call_id,
                    result.content[:4096],
                    int(result.is_error),
                    result.error_kind,
                    "completed" if successful else "unknown",
                    now,
                    self.run_id,
                    idempotency_key,
                ),
            )
            connection.commit()
        finally:
            connection.close()
