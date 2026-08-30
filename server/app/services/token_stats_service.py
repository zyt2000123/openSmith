from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiosqlite

from common import config as common_config
from engine.llm.usage import USAGE_REPORTED_KEY
from engine.observability import ObservabilityReader, TraceIntegrityError

if TYPE_CHECKING:
    from engine.llm.observability import GenerationRecord

from ..infrastructure.database import get_app_db

DbProvider = Callable[[], Awaitable[aiosqlite.Connection]]

# These values describe unavailable or locally derived attribution, not a model.
_NON_MODEL_STAT_KEYS = frozenset({"unknown", "local-estimate"})
# source_key namespaces.  A NULL key means "provider-reported, recorded live";
# everything else says where the row came from *and* whether its numbers are a
# local guess: 'message:{id}' transcript estimate, 'estimate:live:{uuid}' engine
# estimate recorded live, 'estimate:trace:{run}:{seq}' the same imported from a
# trace, '{run}:{seq}' provider-reported and imported from a trace.  The two
# estimate namespaces share a prefix so read paths test one string, and differ
# in origin because the import path deletes its own rows by origin.
_LIVE_ESTIMATE_PREFIX = "estimate:live:"
_TRACE_ESTIMATE_PREFIX = "estimate:trace:"
_ESTIMATE_PREFIXES = ("message:", "estimate:")

# A usage event supersedes a transcript estimate only when the two describe the
# *same turn*.  Turn boundaries are the user messages, so an event and a message
# share a turn exactly when no user message falls between them.
#
# Scoping this per session instead — "this session now has a real usage event,
# drop all of its 'message:' estimates" — also erased every earlier turn the
# engine never priced (a relay that reported no usage, or turns older than the
# feature).  The rows never came back either: the regeneration guard read the
# session as exact and skipped it.  One event could therefore drop a 40-turn
# conversation to whatever that single event was worth.
#
# Both timestamps are UTC ISO-8601 strings, which is what makes these string
# comparisons time comparisons; the rest of this module already relies on that.
_SAME_TURN_USAGE_EXISTS = """
EXISTS (
    SELECT 1 FROM token_usage_events e
    WHERE e.session_id = m.session_id
      AND (e.source_key IS NULL OR e.source_key NOT LIKE 'message:%')
      AND NOT EXISTS (
          SELECT 1 FROM messages b
          WHERE b.session_id = m.session_id AND b.role = 'user'
            AND b.created_at > min(m.created_at, e.occurred_at)
            AND b.created_at <= max(m.created_at, e.occurred_at)
      )
)
"""

# A NULL :session_id means "every session".  The live path binds one because it
# runs once per LLM turn and must not walk the whole table to clean up at most
# its own turn's rows.
_SUPERSEDED_ESTIMATES_DELETE = f"""
DELETE FROM token_usage_events
WHERE source_key LIKE 'message:%'
  AND (:session_id IS NULL OR session_id = :session_id)
  AND EXISTS (
      SELECT 1 FROM messages m
      WHERE 'message:' || m.id = token_usage_events.source_key
        AND {_SAME_TURN_USAGE_EXISTS}
  )
"""

_UNPRICED_TRANSCRIPT_MESSAGES = f"""
SELECT m.id, m.session_id, m.role, m.content, m.created_at
FROM messages m
JOIN sessions s ON s.id = m.session_id
WHERE NOT {_SAME_TURN_USAGE_EXISTS}
  AND NOT EXISTS (
      SELECT 1 FROM token_usage_events prior
      WHERE prior.source_key = 'message:' || m.id
  )
ORDER BY m.created_at ASC
"""

logger = logging.getLogger(__name__)


class TokenStatsService:
    """Persist and aggregate Agent-Smith's local token usage events."""

    def __init__(
        self,
        db_provider: DbProvider = get_app_db,
        *,
        trace_root: Path | None = None,
    ) -> None:
        self._db_provider = db_provider
        self._trace_root = Path(trace_root or common_config.PATHS.agent_dir)
        self._observability = ObservabilityReader(self._trace_root)

    @staticmethod
    def _non_negative_int(value: object) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)) and value >= 0:
            try:
                return int(value)
            except (OverflowError, ValueError):
                # A corrupt trace may carry Infinity (json.loads parses it by
                # default); one such record must not abort the whole import.
                return 0
        return 0

    async def record_usage(
        self,
        *,
        session_id: str,
        run_id: str | None,
        project_name: str,
        project_path: str,
        model: str,
        usage: dict[str, Any] | None,
        occurred_at: datetime | None = None,
    ) -> None:
        if not isinstance(usage, dict):
            return

        input_tokens = self._non_negative_int(usage.get("input_tokens"))
        output_tokens = self._non_negative_int(usage.get("output_tokens"))
        total_tokens = self._non_negative_int(usage.get("total_tokens"))
        if total_tokens == 0:
            total_tokens = input_tokens + output_tokens
        if total_tokens == 0:
            return

        # usage_reported == 0 marks the engine's own estimate, emitted when the
        # provider reported nothing.  Stored with a NULL source_key it would be
        # indistinguishable from provider billing data, and a CJK turn estimated
        # at 3 tokens per character overstates the panel by roughly 3x with no
        # way for a reader to tell.  A missing flag is an older writer, which
        # only ever emitted provider-reported usage.
        estimated = not usage.get(USAGE_REPORTED_KEY, 1)
        source_key = f"{_LIVE_ESTIMATE_PREFIX}{uuid4().hex}" if estimated else None
        db = await self._db_provider()
        await db.execute(
            """
            INSERT INTO token_usage_events (
                session_id, run_id, source_key, project_name, project_path, model,
                input_tokens, output_tokens, total_tokens, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                run_id,
                source_key,
                project_name.strip(),
                project_path.strip(),
                model.strip() or "unknown",
                input_tokens,
                output_tokens,
                total_tokens,
                (occurred_at or datetime.now(timezone.utc)).isoformat(),
            ),
        )
        # A per-turn usage event supersedes the local text-token estimates of
        # *its own turn* (they carry source_key LIKE 'message:%'); clear them in
        # the same transaction so get_stats never double-counts between this
        # call and the next sync_from_traces.  This holds for an engine estimate
        # too: it prices the same turn from the prompt side, so keeping both
        # would count that turn twice.  Earlier turns are a different matter —
        # see _SUPERSEDED_ESTIMATES_DELETE for why they must survive.
        await db.execute(_SUPERSEDED_ESTIMATES_DELETE, {"session_id": session_id})
        # A resumed run (server restarted after the trace was imported) would
        # otherwise keep both its live rows and the trace-imported rows for the
        # whole process lifetime: sync_from_traces only heals at the next startup.
        # Trace-imported rows for this run carry source_key '{run_id}:{seq}', so
        # drop them now that the live path is authoritative for this run again.
        if run_id:
            await db.execute(
                "DELETE FROM token_usage_events "
                "WHERE run_id=? AND source_key IS NOT NULL "
                "AND source_key NOT LIKE 'message:%' "
                # Live estimates for this run are what this method just wrote;
                # only trace-imported rows are the duplicates being healed.
                "AND source_key NOT LIKE 'estimate:live:%'",
                (run_id,),
            )
        await db.commit()

    async def record_generation(self, record: "GenerationRecord") -> None:
        """Persist one generation record; the engine-side sink implementation."""
        usage = record.usage if isinstance(record.usage, dict) else {}
        db = await self._db_provider()
        await db.execute(
            """
            INSERT OR IGNORE INTO llm_generations (
                source_key, session_id, run_id, purpose, provider, model,
                input_tokens, output_tokens, total_tokens,
                cache_read_tokens, cache_write_tokens, reasoning_tokens,
                ttft_ms, total_ms, stream, ok, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.source_key,
                record.session_id,
                record.run_id,
                record.purpose,
                record.provider,
                record.model,
                self._non_negative_int(usage.get("input_tokens")),
                self._non_negative_int(usage.get("output_tokens")),
                self._non_negative_int(usage.get("total_tokens")),
                self._non_negative_int(usage.get("cache_read_tokens")),
                self._non_negative_int(usage.get("cache_write_tokens")),
                self._non_negative_int(usage.get("reasoning_tokens")),
                record.ttft_ms,
                max(0, self._non_negative_int(record.total_ms)),
                1 if record.stream else 0,
                1 if record.ok else 0,
                record.occurred_at,
            ),
        )
        await db.commit()

    async def get_generation_stats(self, year: int | None = None) -> dict[str, Any]:
        """Aggregate generation records by model and purpose, with optional cost.

        Cost is derived read-side from the local ``llm.pricing`` table (USD per
        million tokens); without a price entry a group reports ``cost=None``.
        """
        selected_year = year or datetime.now(timezone.utc).year
        start = date(selected_year, 1, 1)
        end = date(selected_year + 1, 1, 1)
        db = await self._db_provider()
        rows = await db.execute_fetchall(
            """
            SELECT model, purpose,
                   COUNT(*) AS calls,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(total_tokens) AS total_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(cache_write_tokens) AS cache_write_tokens,
                   SUM(reasoning_tokens) AS reasoning_tokens,
                   SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS failed_calls,
                   AVG(CASE WHEN ok=1 THEN total_ms END) AS avg_total_ms,
                   AVG(CASE WHEN ok=1 THEN ttft_ms END) AS avg_ttft_ms
            FROM llm_generations
            WHERE substr(occurred_at, 1, 10) >= ?
              AND substr(occurred_at, 1, 10) < ?
            GROUP BY model, purpose
            ORDER BY total_tokens DESC
            """,
            (start.isoformat(), end.isoformat()),
        )
        price_table = self._load_price_table()
        groups: list[dict[str, Any]] = []
        total_cost: float | None = None
        for row in rows:
            group = {key: row[key] for key in row.keys()}
            for key in ("avg_total_ms", "avg_ttft_ms"):
                group[key] = round(row[key], 1) if row[key] is not None else None
            cost = self._generation_cost(group, price_table.get(str(row["model"])))
            group["cost"] = cost
            if cost is not None:
                total_cost = (total_cost or 0.0) + cost
            groups.append(group)
        return {
            "year": selected_year,
            "groups": groups,
            "total_cost": round(total_cost, 6) if total_cost is not None else None,
        }

    @staticmethod
    def _load_price_table() -> dict[str, dict[str, float]]:
        try:
            from engine.llm.model_config import resolve_price_table

            return resolve_price_table()
        except Exception:
            return {}

    @staticmethod
    def _generation_cost(
        group: dict[str, Any],
        prices: dict[str, float] | None,
    ) -> float | None:
        if not prices:
            return None
        # Cached tokens are billed at their own rate. OpenAI-style usage counts
        # them inside input_tokens, so they are subtracted before pricing the
        # uncached remainder; Anthropic-style usage keeps them separate, where
        # the subtraction clamps to zero and slightly underprices instead of
        # double-charging. Local prices are reference figures either way.
        input_tokens = max(0, int(group.get("input_tokens") or 0))
        cache_read = max(0, int(group.get("cache_read_tokens") or 0))
        uncached_input = max(0, input_tokens - cache_read)
        cost = (
            uncached_input * prices.get("input", 0.0)
            + cache_read * prices.get("cache_read", prices.get("input", 0.0))
            + max(0, int(group.get("output_tokens") or 0)) * prices.get("output", 0.0)
            + max(0, int(group.get("cache_write_tokens") or 0)) * prices.get("cache_write", 0.0)
        ) / 1_000_000
        return round(cost, 6)

    async def sync_from_traces(self) -> int:
        """Import exact token events from durable run traces, once per trace record.

        Trace values that were redacted by older versions are ignored because they
        are not trustworthy numeric usage data. New traces preserve these metrics
        while continuing to redact secrets.
        """
        try:
            return await self._sync_from_traces_inner()
        except Exception:
            # A failed import must not leave the shared connection inside an
            # open write transaction: that would hold the database write lock
            # for the process lifetime and lock out every other writer.
            try:
                await (await self._db_provider()).rollback()
            except Exception:
                pass
            raise

    async def _sync_from_traces_inner(self) -> int:
        runs_dir = self._trace_root / "runs"
        if not runs_dir.is_dir():
            return await self._sync_message_estimates(await self._db_provider())

        def load_run_sessions() -> dict[str, str]:
            result: dict[str, str] = {}
            for path in runs_dir.glob("*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                run_id = payload.get("run_id") or path.stem
                session_id = payload.get("session_id")
                if isinstance(run_id, str) and isinstance(session_id, str) and session_id:
                    result[run_id] = session_id
            return result

        run_sessions = await asyncio.to_thread(load_run_sessions)

        if not run_sessions:
            return await self._sync_message_estimates(await self._db_provider())

        db = await self._db_provider()
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS observability_trace_cursors (
                run_id TEXT PRIMARY KEY,
                byte_offset INTEGER NOT NULL DEFAULT 0,
                project_path TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT 'unknown',
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00','now'))
            )
            """
        )
        # Old traces may reference sessions deleted since; importing them would
        # violate the sessions FK, so they are skipped up front.
        try:
            session_rows = await db.execute_fetchall("SELECT id FROM sessions")
        except aiosqlite.OperationalError:
            session_rows = []
        known_sessions = {str(row["id"]) for row in session_rows}
        cursor_rows = await db.execute_fetchall(
            """
            SELECT run_id, byte_offset, project_path, model
            FROM observability_trace_cursors
            """
        )
        cursors = {
            str(row["run_id"]): {
                "byte_offset": max(0, int(row["byte_offset"] or 0)),
                "project_path": str(row["project_path"] or ""),
                "model": str(row["model"] or "unknown"),
            }
            for row in cursor_rows
        }
        available_trace_ids = set(
            await asyncio.to_thread(self._observability.list_trace_run_ids)
        )
        eligible_run_ids = [
            run_id
            for run_id, session_id in run_sessions.items()
            if session_id in known_sessions and run_id in available_trace_ids
        ]
        stale_cursor_ids = set(cursors).difference(available_trace_ids)
        if stale_cursor_ids:
            await db.executemany(
                "DELETE FROM observability_trace_cursors WHERE run_id=?",
                [(run_id,) for run_id in stale_cursor_ids],
            )

        # Runs whose usage was already recorded live (by record_usage during an
        # SSE stream) must not be re-imported from their traces: the same
        # token_usage event would then exist twice and get_stats would
        # double-count every interactive run after the first restart.  A live
        # estimate counts as recorded — the trace holds that same estimated
        # event, so importing it duplicates the turn.  Auto tasks never call
        # record_usage, so they still arrive through this import.
        live_recorded_run_ids = {
            str(row["run_id"])
            for row in await db.execute_fetchall(
                """
                SELECT DISTINCT run_id FROM token_usage_events
                WHERE (source_key IS NULL OR source_key LIKE 'estimate:live:%')
                  AND run_id IS NOT NULL
                """
            )
        }
        if live_recorded_run_ids:
            # Heal any trace-imported rows already persisted for those runs from
            # an older version that did not skip live-recorded runs.
            await db.executemany(
                """
                DELETE FROM token_usage_events
                WHERE run_id=? AND source_key IS NOT NULL
                  AND source_key NOT LIKE 'message:%'
                  AND source_key NOT LIKE 'estimate:live:%'
                """,
                [(run_id,) for run_id in live_recorded_run_ids],
            )

        def read_new_trace_records():
            batches = []
            for run_id in eligible_run_ids:
                if run_id in live_recorded_run_ids:
                    continue
                cursor = cursors.get(run_id, {})
                try:
                    records, next_offset = self._observability.read_trace_from(
                        run_id,
                        offset=int(cursor.get("byte_offset") or 0),
                    )
                except TraceIntegrityError:
                    logger.warning(
                        "skipping token import from unverifiable trace (run=%s)",
                        run_id,
                    )
                    continue
                batches.append((run_id, records, next_offset))
            return batches

        trace_batches = await asyncio.to_thread(read_new_trace_records)
        imported = 0
        for run_id, records, next_offset in trace_batches:
            session_id = run_sessions.get(run_id)
            if not session_id or session_id not in known_sessions:
                continue
            cursor_state = cursors.get(run_id, {})
            project_path = str(cursor_state.get("project_path") or "")
            model = str(cursor_state.get("model") or "unknown")
            for line_number, record in enumerate(records, start=1):
                event_type = record.get("type")
                data = record.get("data")
                if not isinstance(data, dict):
                    continue
                if event_type == "run_started":
                    candidate = data.get("project_path")
                    if isinstance(candidate, str):
                        project_path = candidate.strip()
                    continue
                if event_type == "raw_response_event":
                    if data.get("type") != "response.created":
                        continue
                    response_data = data.get("data")
                    candidate = response_data.get("model") if isinstance(response_data, dict) else None
                    if isinstance(candidate, str) and candidate.strip():
                        model = candidate.strip()
                    continue
                if event_type != "token_usage":
                    continue

                input_tokens = self._non_negative_int(data.get("input_tokens"))
                output_tokens = self._non_negative_int(data.get("output_tokens"))
                total_tokens = self._non_negative_int(data.get("total_tokens"))
                if total_tokens == 0:
                    total_tokens = input_tokens + output_tokens
                if total_tokens == 0:
                    continue

                timestamp = self._parse_timestamp(record.get("timestamp"))
                # Same rule as the live path: an engine estimate keeps its own
                # namespace so it is never read back as provider billing data.
                # Auto-task runs reach the database only through this import,
                # so leaving it out here would exempt them from the labelling.
                prefix = (
                    _TRACE_ESTIMATE_PREFIX
                    if not data.get(USAGE_REPORTED_KEY, 1)
                    else ""
                )
                source_key = f"{prefix}{run_id}:{record.get('seq', line_number)}"
                cursor = await db.execute(
                    """
                    INSERT OR IGNORE INTO token_usage_events (
                        session_id, run_id, source_key, project_name, project_path, model,
                        input_tokens, output_tokens, total_tokens, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        run_id,
                        source_key,
                        Path(project_path).name if project_path else "",
                        project_path,
                        model,
                        input_tokens,
                        output_tokens,
                        total_tokens,
                        (timestamp or datetime.now(timezone.utc)).isoformat(),
                    ),
                )
                imported += max(cursor.rowcount, 0)
            await db.execute(
                """
                INSERT INTO observability_trace_cursors (
                    run_id, byte_offset, project_path, model, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    byte_offset=excluded.byte_offset,
                    project_path=excluded.project_path,
                    model=excluded.model,
                    updated_at=excluded.updated_at
                """,
                (
                    run_id,
                    max(0, next_offset),
                    project_path,
                    model,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        await db.commit()
        return imported + await self._sync_message_estimates(db)

    async def _sync_message_estimates(self, db: aiosqlite.Connection) -> int:
        """Fill the first dashboard from local transcripts when exact usage is absent.

        This is intentionally marked by a ``message:`` source key. It is a local
        text-token estimate, not provider billing usage, and is replaced turn by
        turn as soon as a usage event prices that turn. Turns the engine never
        priced keep their estimate however many other turns did get one, so a
        session is never reduced to its priced tail.
        """
        try:
            await db.execute(_SUPERSEDED_ESTIMATES_DELETE, {"session_id": None})
            rows = await db.execute_fetchall(_UNPRICED_TRANSCRIPT_MESSAGES)
        except aiosqlite.OperationalError:
            # Keep the service usable with a minimal/custom database in tests or
            # during a partially completed schema migration.
            return 0

        if not rows:
            await db.commit()
            return 0

        try:
            import tiktoken

            encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            encoding = None

        imported = 0
        for row in rows:
            content = str(row["content"] or "")
            if not content.strip():
                continue
            if encoding is not None:
                try:
                    token_count = len(encoding.encode(content, disallowed_special=()))
                except Exception:
                    token_count = max(1, len(content) // 4)
            else:
                token_count = max(1, len(content) // 4)
            input_tokens = token_count if row["role"] != "assistant" else 0
            output_tokens = token_count if row["role"] == "assistant" else 0
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO token_usage_events (
                    session_id, run_id, source_key, project_name, project_path, model,
                    input_tokens, output_tokens, total_tokens, occurred_at
                ) VALUES (?, NULL, ?, '', '', 'local-estimate', ?, ?, ?, ?)
                """,
                (
                    row["session_id"],
                    f"message:{row['id']}",
                    input_tokens,
                    output_tokens,
                    token_count,
                    str(row["created_at"] or datetime.now(timezone.utc).isoformat()),
                ),
            )
            imported += max(cursor.rowcount, 0)
        await db.commit()
        return imported

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    async def get_stats(self, agent_id: str, year: int | None = None) -> dict[str, Any]:
        selected_year = year or datetime.now(timezone.utc).year
        start = date(selected_year, 1, 1)
        end = date(selected_year + 1, 1, 1)
        db = await self._db_provider()
        rows = await db.execute_fetchall(
            """
            SELECT e.session_id, e.model, e.input_tokens, e.output_tokens,
                   e.total_tokens, e.occurred_at, e.source_key
            FROM token_usage_events e
            JOIN sessions s ON s.id = e.session_id
            WHERE s.agent_id=?
              AND substr(e.occurred_at, 1, 10) >= ?
              AND substr(e.occurred_at, 1, 10) < ?
            ORDER BY e.occurred_at ASC
            """,
            (agent_id, start.isoformat(), end.isoformat()),
        )

        daily: dict[str, dict[str, Any]] = {}
        models: dict[str, dict[str, Any]] = {}
        hour_totals: dict[int, int] = {}
        total_input = 0
        total_output = 0
        total_tokens = 0
        sessions: set[str] = set()
        estimated = False

        for row in rows:
            day = str(row["occurred_at"])[:10]
            model = str(row["model"] or "unknown")
            estimated = estimated or str(row["source_key"] or "").startswith(
                _ESTIMATE_PREFIXES
            )
            input_tokens = self._non_negative_int(row["input_tokens"])
            output_tokens = self._non_negative_int(row["output_tokens"])
            event_total = self._non_negative_int(row["total_tokens"])
            if event_total == 0:
                event_total = input_tokens + output_tokens

            day_stat = daily.setdefault(
                day,
                {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "sessions": set()},
            )
            day_stat["input_tokens"] += input_tokens
            day_stat["output_tokens"] += output_tokens
            day_stat["total_tokens"] += event_total
            day_stat["sessions"].add(str(row["session_id"]))

            if model not in _NON_MODEL_STAT_KEYS:
                model_stat = models.setdefault(
                    model,
                    {"model": model, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "sessions": set()},
                )
                model_stat["input_tokens"] += input_tokens
                model_stat["output_tokens"] += output_tokens
                model_stat["total_tokens"] += event_total
                model_stat["sessions"].add(str(row["session_id"]))

            total_input += input_tokens
            total_output += output_tokens
            total_tokens += event_total
            sessions.add(str(row["session_id"]))

            try:
                hour = int(str(row["occurred_at"])[11:13])
            except (TypeError, ValueError):
                hour = None
            if hour is not None and 0 <= hour <= 23:
                hour_totals[hour] = hour_totals.get(hour, 0) + event_total

        daily_output: list[dict[str, Any]] = []
        cursor = start
        while cursor < end:
            key = cursor.isoformat()
            value = daily.get(key, {})
            daily_output.append(
                {
                    "date": key,
                    "input_tokens": int(value.get("input_tokens", 0)),
                    "output_tokens": int(value.get("output_tokens", 0)),
                    "total_tokens": int(value.get("total_tokens", 0)),
                    "sessions": len(value.get("sessions", set())),
                }
            )
            cursor += timedelta(days=1)

        active_dates = [date.fromisoformat(item["date"]) for item in daily_output if item["total_tokens"] > 0]
        current_streak, longest_streak = self._streaks(active_dates)
        model_output = [
            {
                "model": item["model"],
                "input_tokens": item["input_tokens"],
                "output_tokens": item["output_tokens"],
                "total_tokens": item["total_tokens"],
                "sessions": len(item["sessions"]),
            }
            for item in sorted(models.values(), key=lambda value: (-value["total_tokens"], value["model"]))
        ]

        return {
            "year": selected_year,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_tokens": total_tokens,
            "session_count": len(sessions),
            "active_days": len(active_dates),
            "current_streak": current_streak,
            "longest_streak": longest_streak,
            "favorite_model": model_output[0]["model"] if model_output else None,
            "peak_hour": max(hour_totals, key=hour_totals.get) if hour_totals else None,
            "daily": daily_output,
            "models": model_output,
            "estimated": estimated,
        }

    @staticmethod
    def _streaks(active_dates: list[date]) -> tuple[int, int]:
        if not active_dates:
            return 0, 0
        ordered = sorted(set(active_dates))
        longest = current = 1
        for previous, item in zip(ordered, ordered[1:]):
            if item == previous + timedelta(days=1):
                current += 1
            else:
                current = 1
            longest = max(longest, current)
        return current, longest
