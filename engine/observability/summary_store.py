"""Durable, privacy-preserving summaries for completed Agent runs."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from common.paths import PRIVATE_DIR_MODE, PRIVATE_FILE_MODE
from engine.execution.orchestration.run_state import RunStateStore

from .index import (
    IndexedRun,
    ObservabilityIndex,
    ObservabilityRetentionPolicy,
)
from .projections import RunSummary

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SCHEMA_VERSION = 1
logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RunMetadata:
    """Non-sensitive context needed to scope and filter a run summary."""

    run_id: str
    agent_id: str
    created_at: str
    session_id: str | None = None
    identity_id: str | None = None
    route_id: str | None = None
    pipeline_id: str | None = None
    working_dir: str | None = None
    forced_skill: str | None = None


@dataclass(frozen=True)
class RunSummaryRecord:
    """Durable record containing aggregates only, never raw event payloads."""

    schema_version: int
    metadata: RunMetadata
    finished_at: str
    summary: RunSummary

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "metadata": asdict(self.metadata),
            "finished_at": self.finished_at,
            "summary": {
                "run_id": self.summary.run_id,
                "event_count": self.summary.event_count,
                "event_counts": dict(self.summary.event_counts),
                "tool_call_count": self.summary.tool_call_count,
                "backtrack_count": self.summary.backtrack_count,
                "approval_required_count": self.summary.approval_required_count,
                "token_usage": dict(self.summary.token_usage),
                "outcome": self.summary.outcome,
                "reason": self.summary.reason,
            },
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunSummaryRecord":
        metadata = value.get("metadata")
        summary = value.get("summary")
        if not isinstance(metadata, dict) or not isinstance(summary, dict):
            raise ValueError("invalid run summary record")
        run_id = str(metadata.get("run_id") or "")
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError("invalid run summary id")
        event_counts = summary.get("event_counts")
        token_usage = summary.get("token_usage")
        if not isinstance(event_counts, dict) or not isinstance(token_usage, dict):
            raise ValueError("invalid run summary aggregates")
        return cls(
            schema_version=int(value.get("schema_version", 0)),
            metadata=RunMetadata(
                run_id=run_id,
                agent_id=str(metadata.get("agent_id") or ""),
                created_at=str(metadata.get("created_at") or ""),
                session_id=_optional_text(metadata.get("session_id")),
                identity_id=_optional_text(metadata.get("identity_id")),
                route_id=_optional_text(metadata.get("route_id")),
                pipeline_id=_optional_text(metadata.get("pipeline_id")),
                working_dir=_optional_text(metadata.get("working_dir")),
                forced_skill=_optional_text(metadata.get("forced_skill")),
            ),
            finished_at=str(value.get("finished_at") or ""),
            summary=RunSummary(
                run_id=run_id,
                event_count=_non_negative_int(summary.get("event_count")),
                event_counts={str(key): _non_negative_int(item) for key, item in event_counts.items()},
                tool_call_count=_non_negative_int(summary.get("tool_call_count")),
                backtrack_count=_non_negative_int(summary.get("backtrack_count")),
                approval_required_count=_non_negative_int(summary.get("approval_required_count")),
                token_usage={str(key): _non_negative_int(item) for key, item in token_usage.items()},
                outcome=_optional_text(summary.get("outcome")),
                reason=_optional_text(summary.get("reason")),
            ),
        )


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None and str(value) else None


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


class RunSummaryStore:
    """Store and list aggregate run outcomes under the local profile directory."""

    def __init__(
        self,
        profile_dir: Path,
        *,
        retention: ObservabilityRetentionPolicy | None = None,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.root = self.profile_dir / "runs"
        self.trace_root = self.profile_dir / "traces"
        self.root.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
        self.root.chmod(PRIVATE_DIR_MODE)
        self._retention = retention or ObservabilityRetentionPolicy.from_environment()
        # A failed index write marks the index stale.  bootstrap() is idempotent
        # (ON CONFLICT upserts), so the next _ensure_index() reconciles the
        # previously-failed row instead of leaving the run invisible to list()
        # forever.
        self._index_stale = False
        try:
            self._index: ObservabilityIndex | None = ObservabilityIndex(
                self.profile_dir
            )
        except (OSError, sqlite3.Error):
            logger.warning("failed to initialize observability index", exc_info=True)
            self._index = None

    def save(
        self,
        metadata: RunMetadata,
        summary: RunSummary,
        *,
        finished_at: str | None = None,
    ) -> RunSummaryRecord:
        """Atomically persist a terminal summary, merging earlier resumptions.

        ``finished_at`` defaults to now, which is right for a run finishing
        now.  Crash recovery must pass the run's own last-known time instead:
        retention orders by this field, so stamping a months-old run with the
        current time sorts it ahead of every real run and evicts them.
        """
        self._validate_metadata(metadata, summary)
        self._ensure_index()
        previous = self.get(metadata.run_id)
        merged = _merge_summaries(previous.summary, summary) if previous is not None else summary
        record = RunSummaryRecord(
            schema_version=_SCHEMA_VERSION,
            metadata=(
                _merge_metadata(previous.metadata, metadata)
                if previous is not None
                else metadata
            ),
            finished_at=finished_at or _now(),
            summary=merged,
        )
        self._write(record)
        if self._index is not None:
            try:
                self._index.upsert(self._index_entry(record))
                self._apply_retention()
                self._index_stale = False
            except (OSError, sqlite3.Error):
                logger.warning(
                    "failed to update observability index (run=%s)",
                    metadata.run_id,
                    exc_info=True,
                )
                self._index_stale = True
        return record

    def get(self, run_id: str) -> RunSummaryRecord | None:
        path = self._path(run_id)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return RunSummaryRecord.from_dict(value) if isinstance(value, dict) else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def list(self, agent_id: str, *, limit: int = 50) -> list[RunSummaryRecord]:
        if limit < 1:
            return []
        self._ensure_index()
        if self._index is not None:
            try:
                records = [
                    record
                    for run_id in self._index.list_run_ids(agent_id, limit=limit)
                    if (record := self.get(run_id)) is not None
                ]
                return records[:limit]
            except (OSError, sqlite3.Error):
                logger.warning("failed to query observability index", exc_info=True)
        records = [
            record
            for path in self.root.glob("*.summary.json")
            if (record := self._read_path(path)) is not None and record.metadata.agent_id == agent_id
        ]
        return sorted(records, key=lambda record: record.finished_at, reverse=True)[:limit]

    def _ensure_index(self) -> None:
        if self._index is None:
            return
        try:
            if self._index.is_bootstrapped() and not self._index_stale:
                return
            entries = [
                self._index_entry(record)
                for path in self.root.glob("*.summary.json")
                if (record := self._read_path(path)) is not None
            ]
            self._index.bootstrap(entries)
            self._index_stale = False
        except (OSError, sqlite3.Error):
            logger.warning("failed to bootstrap observability index", exc_info=True)
            self._index = None

    def _index_entry(self, record: RunSummaryRecord) -> IndexedRun:
        summary_path = self._path(record.metadata.run_id)
        trace_path = self.trace_root / f"{record.metadata.run_id}.jsonl"
        size_bytes = 0
        for path in (summary_path, trace_path):
            try:
                size_bytes += path.stat().st_size
            except OSError:
                continue
        return IndexedRun(
            run_id=record.metadata.run_id,
            agent_id=record.metadata.agent_id,
            session_id=record.metadata.session_id,
            identity_id=record.metadata.identity_id,
            created_at=record.metadata.created_at,
            finished_at=record.finished_at,
            size_bytes=size_bytes,
        )

    def _apply_retention(self) -> None:
        if self._index is None:
            return
        candidates = self._index.retention_candidates(self._retention)
        if not candidates:
            return
        state_store = RunStateStore(self.profile_dir)
        for run_id in candidates:
            try:
                # Liveness first, and it gates *everything*.  A resumed run
                # still carries its previous attempt's finished_at here, so it
                # can be selected as a candidate mid-execution; deleting its
                # trace out from under it is worse than losing the state file.
                # TraceStore reopens the path with O_CREAT on the next append
                # and its torn-tail check sees a shorter file, so the chain
                # silently restarts from genesis -- the run's own history is
                # gone and verify() still answers ok.  The summary going first
                # also breaks the resumption merge save() promises: with no
                # previous record there is nothing to merge, and the earlier
                # attempt's counts vanish.
                if not state_store.prune(run_id):
                    continue
                # Everything belonging to the run goes, not just the two files
                # this store writes.  Leaving the lifecycle state behind made
                # "terminal state, no summary" ambiguous, and startup
                # reconciliation reads that as a crash window: it re-finalized
                # every pruned run into an empty zombie summary stamped with the
                # current time, which sorted ahead of real runs and evicted
                # them -- the evicted ones then came back as zombies on the next
                # start, so the index converged on holding nothing but zombies.
                # The orphaned seal anchor is also what made the rebuilt trace
                # verify as tampered.
                self._path(run_id).unlink(missing_ok=True)
                (self.trace_root / f"{run_id}.jsonl").unlink(missing_ok=True)
                (self.trace_root / f"{run_id}.jsonl.head").unlink(missing_ok=True)
                (self.trace_root / f"{run_id}.jsonl.torn").unlink(missing_ok=True)
                self._index.remove(run_id)
            except (OSError, sqlite3.Error, ValueError):
                logger.warning(
                    "failed to prune observed run %s",
                    run_id,
                    exc_info=True,
                )

    def _read_path(self, path: Path) -> RunSummaryRecord | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return RunSummaryRecord.from_dict(value) if isinstance(value, dict) else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _write(self, record: RunSummaryRecord) -> None:
        path = self._path(record.metadata.run_id)
        temp_path = self.root / f".{record.metadata.run_id}.{uuid4().hex}.tmp"
        payload = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        try:
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            path.chmod(PRIVATE_FILE_MODE)
        finally:
            temp_path.unlink(missing_ok=True)

    @staticmethod
    def _validate_metadata(metadata: RunMetadata, summary: RunSummary) -> None:
        if not _RUN_ID_RE.fullmatch(metadata.run_id) or summary.run_id != metadata.run_id:
            raise ValueError("invalid run summary id")
        if not metadata.agent_id:
            raise ValueError("run summary requires an agent id")

    def _path(self, run_id: str) -> Path:
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError("invalid run summary id")
        return self.root / f"{run_id}.summary.json"


def _merge_summaries(previous: RunSummary, current: RunSummary) -> RunSummary:
    """Keep one run-level aggregate across a cancelled run and its resume."""
    return RunSummary(
        run_id=current.run_id,
        event_count=previous.event_count + current.event_count,
        event_counts=_sum_maps(previous.event_counts, current.event_counts),
        tool_call_count=previous.tool_call_count + current.tool_call_count,
        backtrack_count=previous.backtrack_count + current.backtrack_count,
        approval_required_count=previous.approval_required_count + current.approval_required_count,
        token_usage=_sum_maps(previous.token_usage, current.token_usage),
        outcome=current.outcome,
        reason=current.reason,
    )


def _merge_metadata(previous: RunMetadata, current: RunMetadata) -> RunMetadata:
    """Preserve the original scope while filling metadata resolved after start."""
    return RunMetadata(
        run_id=previous.run_id,
        agent_id=previous.agent_id,
        created_at=previous.created_at,
        session_id=previous.session_id or current.session_id,
        identity_id=previous.identity_id or current.identity_id,
        route_id=previous.route_id or current.route_id,
        pipeline_id=previous.pipeline_id or current.pipeline_id,
        working_dir=previous.working_dir or current.working_dir,
        forced_skill=previous.forced_skill or current.forced_skill,
    )


def _sum_maps(left: Any, right: Any) -> dict[str, int]:
    keys = set(left) | set(right)
    return {str(key): _non_negative_int(left.get(key)) + _non_negative_int(right.get(key)) for key in keys}
