"""Small durable index and retention policy for completed run observations."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from common.paths import PRIVATE_DIR_MODE, PRIVATE_FILE_MODE

_DEFAULT_MAX_COMPLETED_RUNS = 2_000
_DEFAULT_MAX_AGE_DAYS = 90
_DEFAULT_MAX_BYTES = 512 * 1024 * 1024


def _optional_positive_env(name: str, default: int) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    if parsed == 0:
        return None
    return parsed if parsed > 0 else default


@dataclass(frozen=True)
class ObservabilityRetentionPolicy:
    """Bounds for completed summaries and traces.

    ``None`` disables one bound. Environment values of ``0`` likewise disable
    that bound for the default runtime policy.
    """

    max_completed_runs: int | None = _DEFAULT_MAX_COMPLETED_RUNS
    max_age_days: int | None = _DEFAULT_MAX_AGE_DAYS
    max_bytes: int | None = _DEFAULT_MAX_BYTES

    def __post_init__(self) -> None:
        for name, value in (
            ("max_completed_runs", self.max_completed_runs),
            ("max_age_days", self.max_age_days),
            ("max_bytes", self.max_bytes),
        ):
            if value is not None and value < 1:
                raise ValueError(f"{name} must be positive or None")

    @classmethod
    def from_environment(cls) -> "ObservabilityRetentionPolicy":
        return cls(
            max_completed_runs=_optional_positive_env(
                "AGENT_SMITH_OBSERVABILITY_MAX_RUNS",
                _DEFAULT_MAX_COMPLETED_RUNS,
            ),
            max_age_days=_optional_positive_env(
                "AGENT_SMITH_OBSERVABILITY_MAX_AGE_DAYS",
                _DEFAULT_MAX_AGE_DAYS,
            ),
            max_bytes=_optional_positive_env(
                "AGENT_SMITH_OBSERVABILITY_MAX_BYTES",
                _DEFAULT_MAX_BYTES,
            ),
        )


@dataclass(frozen=True)
class IndexedRun:
    run_id: str
    agent_id: str
    created_at: str
    finished_at: str
    session_id: str | None = None
    identity_id: str | None = None
    size_bytes: int = 0


class ObservabilityIndex:
    """SQLite metadata index; observation payloads remain in existing files."""

    def __init__(self, profile_dir: Path) -> None:
        self.root = Path(profile_dir) / "observability"
        self.root.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
        self.root.chmod(PRIVATE_DIR_MODE)
        self.path = self.root / "index.sqlite3"
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS completed_runs (
                    run_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    session_id TEXT,
                    identity_id TEXT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_completed_runs_agent_finished
                    ON completed_runs(agent_id, finished_at DESC);
                """
            )
        self.path.chmod(PRIVATE_FILE_MODE)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5.0)
        try:
            db.execute("PRAGMA busy_timeout=5000")
            # WAL allows concurrent reader/writer access across parallel agent
            # runs sharing one index.sqlite3.  Without it a busy writer can
            # raise SQLITE_BUSY inside a 5s window; save() treats that as a
            # best-effort failure and the run silently stays out of list()
            # forever (bootstrap only ever runs once).
            db.execute("PRAGMA journal_mode=WAL")
            yield db
            db.commit()
        finally:
            db.close()

    def is_bootstrapped(self) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT value FROM metadata WHERE key='summary_files_bootstrapped'"
            ).fetchone()
        return bool(row and row[0] == "1")

    def bootstrap(self, entries: list[IndexedRun]) -> None:
        with self._connect() as db:
            db.executemany(
                """
                INSERT INTO completed_runs (
                    run_id, agent_id, session_id, identity_id,
                    created_at, finished_at, size_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    agent_id=excluded.agent_id,
                    session_id=excluded.session_id,
                    identity_id=excluded.identity_id,
                    created_at=excluded.created_at,
                    finished_at=excluded.finished_at,
                    size_bytes=excluded.size_bytes
                """,
                [
                    (
                        entry.run_id,
                        entry.agent_id,
                        entry.session_id,
                        entry.identity_id,
                        entry.created_at,
                        entry.finished_at,
                        max(0, entry.size_bytes),
                    )
                    for entry in entries
                ],
            )
            db.execute(
                """
                INSERT INTO metadata(key, value)
                VALUES ('summary_files_bootstrapped', '1')
                ON CONFLICT(key) DO UPDATE SET value='1'
                """
            )

    def upsert(self, entry: IndexedRun) -> None:
        self.bootstrap_entry(entry)

    def bootstrap_entry(self, entry: IndexedRun) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO completed_runs (
                    run_id, agent_id, session_id, identity_id,
                    created_at, finished_at, size_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    agent_id=excluded.agent_id,
                    session_id=excluded.session_id,
                    identity_id=excluded.identity_id,
                    created_at=excluded.created_at,
                    finished_at=excluded.finished_at,
                    size_bytes=excluded.size_bytes
                """,
                (
                    entry.run_id,
                    entry.agent_id,
                    entry.session_id,
                    entry.identity_id,
                    entry.created_at,
                    entry.finished_at,
                    max(0, entry.size_bytes),
                ),
            )

    def list_run_ids(self, agent_id: str, *, limit: int) -> list[str]:
        if limit < 1:
            return []
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT run_id
                FROM completed_runs
                WHERE agent_id=?
                ORDER BY finished_at DESC, rowid DESC
                LIMIT ?
                """,
                (agent_id, limit),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def retention_candidates(
        self,
        policy: ObservabilityRetentionPolicy,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT run_id, finished_at, size_bytes
                FROM completed_runs
                ORDER BY finished_at DESC, rowid DESC
                """
            ).fetchall()

        cutoff = None
        if policy.max_age_days is not None:
            cutoff = (now or datetime.now(timezone.utc)) - timedelta(
                days=policy.max_age_days
            )
        retained_bytes = 0
        retained_count = 0
        candidates: list[str] = []
        for index, (run_id, finished_at, size_bytes) in enumerate(rows):
            too_many = (
                policy.max_completed_runs is not None
                and index >= policy.max_completed_runs
            )
            too_old = False
            if cutoff is not None:
                try:
                    parsed = datetime.fromisoformat(str(finished_at))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    too_old = parsed < cutoff
                except ValueError:
                    too_old = False
            entry_bytes = max(0, int(size_bytes or 0))
            too_large = (
                policy.max_bytes is not None
                and retained_count > 0
                and retained_bytes + entry_bytes > policy.max_bytes
            )
            if too_many or too_old or too_large:
                candidates.append(str(run_id))
            else:
                retained_bytes += entry_bytes
                retained_count += 1
        return candidates

    def remove(self, run_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM completed_runs WHERE run_id=?", (run_id,))
