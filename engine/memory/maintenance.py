"""Memory-owned lifecycle maintenance.

This module owns the runtime-facing policy for compilation, periodic candidate
nudges, and Dream reconciliation while accepting the required LLM clients from
the execution composition root.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, ClassVar

from engine.llm.port import LLMPort
from engine.memory._files import async_interprocess_file_lock, atomic_write_text

logger = logging.getLogger(__name__)
# Three policy views may each consume a generator/reviewer round. This timeout
# is for explicit/idle maintenance; production turn finalization defers this
# work when the runtime owns shared LLM clients.
_MEMORY_MAINTENANCE_TIMEOUT_SECONDS = 900.0
_COMPILE_PENDING_FILE = ".compile_pending"
_NUDGE_PENDING_FILE = ".nudge_pending"
_DREAM_PENDING_FILE = ".dream_pending"
MAINTENANCE_KINDS: tuple[str, ...] = ("compile", "nudge", "dream")

# Review/content rejections deserve a fresh attempt soon; transport/provider
# failures (timeouts, unreachable providers, disk errors) back off so a
# due-but-failing lane cannot hammer the LLM every turn.
_REVIEW_REJECTION_MARKERS = (
    "did not pass review",
    "contains sensitive information",
    "instruction-injection",
    "exceeded character budget",
    "LLM returned insufficient output",
    "requires a reviewer",
)


def _failure_needs_backoff(
    *,
    exc: BaseException | None = None,
    error_text: str | None = None,
) -> bool:
    """Whether a maintenance failure should enter the retry cooldown."""
    if exc is not None:
        from engine.memory._review import MemoryCompilationError
        from engine.memory.nudge import MemoryNudgeError
        from engine.memory.policy import MemoryPolicyError

        if isinstance(exc, (MemoryCompilationError, MemoryNudgeError, MemoryPolicyError)):
            return False
        return True
    if error_text is not None:
        return not any(
            marker in error_text for marker in _REVIEW_REJECTION_MARKERS
        )
    return True


@dataclass(frozen=True)
class MemoryMaintenanceService:
    """Execute memory rules with externally owned runtime dependencies."""

    llm: LLMPort
    reviewer: LLMPort | None = None
    defer_maintenance: bool = False

    _locks: ClassVar[dict[Path, asyncio.Lock]] = {}
    _background_tasks: ClassVar[dict[tuple[Path, str], asyncio.Task[None]]] = {}

    async def record_turn(
        self,
        agent_dir: Path,
        user_message: str,
        reply_text: str,
        had_tools: bool,
        learning_signals: list[str] | None = None,
        *,
        turn_status: str = "completed",
        turn_reason: str | None = None,
    ) -> bool:
        """Persist turn evidence and run threshold-based maintenance."""
        memory_dir = agent_dir / "memory"
        async with self._operation_lock(memory_dir):
            try:
                from engine.memory.store import save_conversation_memory

                compile_maintenance = (
                    self._schedule_compilation
                    if self.defer_maintenance
                    else self._run_compilation_unlocked
                )
                dream_maintenance = (
                    self._schedule_dream
                    if self.defer_maintenance
                    else self._run_dream_unlocked
                )
                nudge_maintenance = (
                    self._schedule_nudge
                    if self.defer_maintenance
                    else self._run_nudge_and_compile_unlocked
                )
                await save_conversation_memory(
                    agent_dir,
                    user_message,
                    reply_text,
                    had_tools,
                    learning_signals=learning_signals,
                    turn_status=turn_status,
                    turn_reason=turn_reason,
                    compile_maintenance=compile_maintenance,
                    nudge_maintenance=nudge_maintenance,
                    dream_maintenance=dream_maintenance,
                )
                return True
            except Exception:
                logger.warning("conversation-memory lifecycle hook failed", exc_info=True)
                return False

    async def run_compile(self, memory_dir: Path) -> bool:
        """Compile recent and durable memory for an explicit trigger."""
        async with self._operation_lock(memory_dir):
            completed = await self._run_compilation_unlocked(memory_dir)
            if completed:
                self._mark_completed("compile", memory_dir)
            return completed

    async def run_dream(self, memory_dir: Path) -> bool:
        """Run Dream maintenance for an explicit trigger."""
        async with self._operation_lock(memory_dir):
            completed = await self._run_dream_unlocked(memory_dir)
            if completed:
                self._mark_completed("dream", memory_dir)
            return completed

    async def run_nudge(self, memory_dir: Path) -> bool:
        """Run one periodic candidate-discovery cycle explicitly."""
        async with self._operation_lock(memory_dir):
            completed = await self._run_nudge_and_compile_unlocked(memory_dir)
            if completed:
                self._mark_completed("nudge", memory_dir)
            return completed

    async def run_idle_maintenance(self, memory_dir: Path) -> bool:
        """Retry only maintenance that was due or previously left pending."""
        async with self._operation_lock(memory_dir):
            compiled = True
            nudged = True
            dreamed = True
            if self._is_pending("nudge", memory_dir):
                nudged = await self._run_nudge_and_compile_unlocked(memory_dir)
                if nudged:
                    self._mark_completed("nudge", memory_dir)
            topic_sync_pending = False
            try:
                from engine.memory.knowledge import TopicAssociationStore
                topic_sync_pending = TopicAssociationStore(memory_dir).is_sync_pending()
            except Exception:
                logger.warning("topic sync retry state unavailable", exc_info=True)
            if self._is_pending("compile", memory_dir) or topic_sync_pending:
                compiled = await self._run_compilation_unlocked(memory_dir)
                if compiled:
                    self._mark_completed("compile", memory_dir)
            if self._is_pending("dream", memory_dir):
                dreamed = await self._run_dream_unlocked(memory_dir)
                if dreamed:
                    self._mark_completed("dream", memory_dir)
            return compiled and nudged and dreamed

    async def _run_compilation_unlocked(self, memory_dir: Path) -> bool:
        from engine.memory.store import _in_retry_cooldown, _record_retry_attempt

        if _in_retry_cooldown(memory_dir, "compile"):
            return False
        try:
            from engine.memory.compile import run_compilation

            report = await asyncio.wait_for(
                run_compilation(
                    memory_dir,
                    self.llm,
                    reviewer=self.reviewer,
                    raise_on_error=True,
                    allow_partial_progress=True,
                    return_diagnostics=True,
                    sync_topics=True,
                ),
                timeout=_MEMORY_MAINTENANCE_TIMEOUT_SECONDS,
            )
            result = report["results"]
            errors = report["errors"]
            if result.get("recent") and not result.get("durable"):
                logger.info("recent memory compiled; durable memory remains pending review")
            return not errors
        except Exception as exc:
            logger.warning("conversation-memory compilation failed", exc_info=True)
            if _failure_needs_backoff(exc=exc):
                _record_retry_attempt(memory_dir, "compile")
            return False

    async def _schedule_compilation(self, memory_dir: Path) -> bool:
        self._mark_pending("compile", memory_dir)
        self._schedule_background("compile", memory_dir)
        return False

    async def _schedule_nudge(self, memory_dir: Path) -> bool:
        self._mark_pending("nudge", memory_dir)
        self._schedule_background("nudge", memory_dir)
        return False

    async def _schedule_dream(self, memory_dir: Path) -> bool:
        self._mark_pending("dream", memory_dir)
        self._schedule_background("dream", memory_dir)
        return False

    def _schedule_background(self, kind: str, memory_dir: Path) -> None:
        from engine.memory.store import _in_retry_cooldown

        key = (memory_dir.resolve(), kind)
        existing = self._background_tasks.get(key)
        if existing is not None and not existing.done():
            return
        if _in_retry_cooldown(memory_dir, kind):
            # A recently failed attempt is inside its cooldown; do not spawn a
            # fresh background task that would hit the provider again this turn.
            return

        runners = {
            "compile": self._run_background_compilation,
            "nudge": self._run_background_nudge,
            "dream": self._run_background_dream,
        }
        runner = runners[kind]
        task = asyncio.create_task(runner(memory_dir))
        self._background_tasks[key] = task

        def finish(completed: asyncio.Task[None]) -> None:
            self._discard_completed_task(key, completed)
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("background memory %s failed", kind, exc_info=True)

        task.add_done_callback(finish)

    @classmethod
    def _discard_completed_task(
        cls,
        key: tuple[Path, str],
        completed: asyncio.Task[None],
    ) -> None:
        """Pop a finished task only when it is still the registered one.

        The done-callback of an old task may run after a new task was registered
        under the same key; popping unconditionally would silently evict the new
        task and leave it running unobserved.
        """
        if cls._background_tasks.get(key) is completed:
            cls._background_tasks.pop(key, None)

    async def _run_background_compilation(self, memory_dir: Path) -> None:
        async with self._operation_lock(memory_dir):
            if await self._run_compilation_unlocked(memory_dir):
                self._mark_completed("compile", memory_dir)

    async def _run_background_nudge(self, memory_dir: Path) -> None:
        async with self._operation_lock(memory_dir):
            if await self._run_nudge_and_compile_unlocked(memory_dir):
                self._mark_completed("nudge", memory_dir)

    async def _run_background_dream(self, memory_dir: Path) -> None:
        async with self._operation_lock(memory_dir):
            if await self._run_dream_unlocked(memory_dir):
                self._mark_completed("dream", memory_dir)

    async def _run_nudge_and_compile_unlocked(self, memory_dir: Path) -> bool:
        """Discover candidates, then reuse the normal compiler if any were found.

        The nudge owns only candidate discovery.  It marks compilation pending
        and invokes the existing compiler; no code in this method writes a
        durable view directly.
        """
        from engine.memory.store import _in_retry_cooldown, _record_retry_attempt

        if _in_retry_cooldown(memory_dir, "nudge"):
            return False
        try:
            from engine.memory.nudge import run_nudge

            report = await asyncio.wait_for(
                run_nudge(memory_dir, self.llm, reviewer=self.reviewer),
                timeout=_MEMORY_MAINTENANCE_TIMEOUT_SECONDS,
            )
            if not report.completed:
                reason = report.error or report.status
                logger.warning("periodic memory nudge did not complete: %s", reason)
                if report.status == "failed":
                    _record_retry_attempt(memory_dir, "nudge")
                return False
            if report.candidates_written:
                self._mark_pending("compile", memory_dir)
                if await self._run_compilation_unlocked(memory_dir):
                    self._mark_completed("compile", memory_dir)
            return True
        except Exception as exc:
            try:
                from engine.memory.history import append_memory_history
                from engine.memory.policy import load_memory_policy

                append_memory_history(
                    memory_dir,
                    target="nudge",
                    policy_version=load_memory_policy().version,
                    status="failed",
                    error=f"maintenance: {type(exc).__name__}: {exc}",
                )
            except Exception:
                logger.warning("could not audit periodic nudge failure", exc_info=True)
            logger.warning("periodic memory nudge failed", exc_info=True)
            if _failure_needs_backoff(exc=exc):
                _record_retry_attempt(memory_dir, "nudge")
            return False

    async def wait_for_pending_tasks(self, memory_dir: Path) -> None:
        """Wait for currently scheduled maintenance; primarily useful to callers/tests."""
        resolved = memory_dir.resolve()
        tasks = [
            task
            for (path, _), task in self._background_tasks.items()
            if path == resolved and not task.done()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_dream_unlocked(self, memory_dir: Path) -> bool:
        from engine.memory.store import _in_retry_cooldown, _record_retry_attempt

        if _in_retry_cooldown(memory_dir, "dream"):
            return False
        try:
            from engine.memory.dream import dream_report_completed, run_dream

            report = await asyncio.wait_for(
                run_dream(memory_dir, self.llm, reviewer=self.reviewer),
                timeout=_MEMORY_MAINTENANCE_TIMEOUT_SECONDS,
            )
            # Dream runs on a low-frequency cadence: apply audit-log retention here.
            try:
                from engine.memory.history import trim_memory_history

                trim_memory_history(memory_dir)
            except Exception:
                logger.warning("could not trim memory history", exc_info=True)
            if not dream_report_completed(report):
                reason = "; ".join(report.errors) if report.errors else report.skipped
                logger.warning("conversation-memory Dream did not complete: %s", reason)
                if _failure_needs_backoff(error_text=reason):
                    _record_retry_attempt(memory_dir, "dream")
                return False
            return True
        except Exception as exc:
            try:
                from engine.memory.dream import _record_dream_failure

                _record_dream_failure(
                    memory_dir,
                    f"maintenance: {type(exc).__name__}: {exc}",
                )
            except Exception:
                logger.warning("could not audit Dream maintenance failure", exc_info=True)
            logger.warning("conversation-memory Dream consolidation failed", exc_info=True)
            if _failure_needs_backoff(exc=exc):
                _record_retry_attempt(memory_dir, "dream")
            return False

    @classmethod
    def _lock_for(cls, memory_dir: Path) -> asyncio.Lock:
        key = memory_dir.resolve()
        lock = cls._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            cls._locks[key] = lock
        return lock

    @classmethod
    @asynccontextmanager
    async def _operation_lock(cls, memory_dir: Path) -> AsyncIterator[None]:
        async with cls._lock_for(memory_dir):
            async with async_interprocess_file_lock(memory_dir / ".maintenance"):
                yield

    @staticmethod
    def _pending_path(kind: str, memory_dir: Path) -> Path:
        if kind == "compile":
            return memory_dir / _COMPILE_PENDING_FILE
        if kind == "nudge":
            return memory_dir / _NUDGE_PENDING_FILE
        if kind == "dream":
            return memory_dir / _DREAM_PENDING_FILE
        raise ValueError(f"unknown memory maintenance kind: {kind}")

    @classmethod
    def _mark_pending(cls, kind: str, memory_dir: Path) -> None:
        memory_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(cls._pending_path(kind, memory_dir), "1")

    @classmethod
    def _clear_pending(cls, kind: str, memory_dir: Path) -> None:
        cls._pending_path(kind, memory_dir).unlink(missing_ok=True)

    @classmethod
    def _mark_completed(cls, kind: str, memory_dir: Path) -> None:
        from engine.memory.store import _clear_retry_attempt, _reset_counter

        cls._clear_pending(kind, memory_dir)
        _reset_counter(memory_dir / f".{kind}_counter")
        _clear_retry_attempt(memory_dir, kind)

    @classmethod
    def maintenance_status(cls, memory_dir: Path) -> dict[str, str]:
        """Report, per maintenance kind, whether work is running or still owed.

        ``running`` means a background task for it is in flight in this process.
        ``pending`` means the work is owed — a marker file, or a counter past its
        threshold — but nothing is executing it yet; the scheduler's idle tick
        picks those up. Deferred maintenance is otherwise invisible: it spans
        turns, so it cannot be reported over a per-run event stream.
        """
        resolved = memory_dir.resolve()
        status: dict[str, str] = {}
        for kind in MAINTENANCE_KINDS:
            task = cls._background_tasks.get((resolved, kind))
            if task is not None and not task.done():
                status[kind] = "running"
            elif cls._is_pending(kind, memory_dir):
                status[kind] = "pending"
            else:
                status[kind] = "idle"
        return status

    @classmethod
    def _is_pending(cls, kind: str, memory_dir: Path) -> bool:
        if cls._pending_path(kind, memory_dir).is_file():
            return True
        try:
            if kind == "compile":
                from engine.memory.store import _COMPILE_INTERVAL

                threshold = _COMPILE_INTERVAL
            elif kind == "nudge":
                from engine.memory.nudge import NUDGE_INTERVAL

                threshold = NUDGE_INTERVAL
            elif kind == "dream":
                from engine.memory.dream import DREAM_INTERVAL

                threshold = DREAM_INTERVAL
            else:
                raise ValueError(f"unknown memory maintenance kind: {kind}")
            counter = int((memory_dir / f".{kind}_counter").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            # A malformed counter should be retried and repaired, never suppress
            # maintenance indefinitely.
            return True
        return counter >= threshold


@dataclass(frozen=True)
class MemoryLifecycleHooks:
    """Hook adapter for memory lifecycle events."""

    maintenance: MemoryMaintenanceService

    async def memory_after_turn_completed(
        self,
        agent_dir: Path,
        user_message: str,
        reply_text: str,
        had_tools: bool,
        learning_signals: list[str] | None = None,
    ) -> bool:
        return await self.maintenance.record_turn(
            agent_dir,
            user_message,
            reply_text,
            had_tools,
            learning_signals,
        )

    async def memory_after_turn_incomplete(
        self,
        agent_dir: Path,
        user_message: str,
        reply_text: str,
        had_tools: bool,
        learning_signals: list[str] | None = None,
        reason: str | None = None,
    ) -> bool:
        """Persist partial work without promoting it to completed memory."""
        return await self.maintenance.record_turn(
            agent_dir,
            user_message,
            reply_text,
            had_tools,
            learning_signals,
            turn_status="incomplete",
            turn_reason=reason,
        )

    async def memory_after_turn_failed(
        self,
        agent_dir: Path,
        user_message: str,
        reply_text: str,
        had_tools: bool,
        learning_signals: list[str] | None = None,
        reason: str | None = None,
    ) -> bool:
        """Persist partial work from a failed run with an explicit status."""
        return await self.maintenance.record_turn(
            agent_dir,
            user_message,
            reply_text,
            had_tools,
            learning_signals,
            turn_status="failed",
            turn_reason=reason,
        )

    async def memory_idle_tick(self, memory_dir: Path) -> bool:
        return await self.maintenance.run_idle_maintenance(memory_dir)

    async def memory_daily_tick(self, memory_dir: Path) -> bool:
        return await self.maintenance.run_idle_maintenance(memory_dir)


def memory_maintenance_status(memory_dir: Path) -> dict[str, str]:
    """Read-only view of deferred memory maintenance, for status endpoints.

    Deliberately free of LLM dependencies: a status probe must not have to build
    provider clients just to answer whether compilation or dreaming is in flight.
    """
    return MemoryMaintenanceService.maintenance_status(memory_dir)
