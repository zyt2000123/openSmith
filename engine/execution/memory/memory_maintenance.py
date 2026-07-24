"""Lifecycle-owned memory maintenance.

The memory package stays storage/compilation focused. This module owns the
runtime-facing policy: when a lifecycle hook fires, run memory maintenance with
the LLM clients already held by RuntimeServices.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from engine.llm.port import LLMPort
from engine.memory._files import atomic_write_text

logger = logging.getLogger(__name__)
# Three policy views may each consume a generator/reviewer round. This timeout
# is for explicit/idle maintenance; production turn finalization defers this
# work when the runtime owns shared LLM clients.
_MEMORY_MAINTENANCE_TIMEOUT_SECONDS = 900.0
_COMPILE_PENDING_FILE = ".compile_pending"
_DREAM_PENDING_FILE = ".dream_pending"


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
        lock = self._lock_for(memory_dir)
        async with lock:
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
                await save_conversation_memory(
                    agent_dir,
                    user_message,
                    reply_text,
                    had_tools,
                    learning_signals=learning_signals,
                    turn_status=turn_status,
                    turn_reason=turn_reason,
                    compile_maintenance=compile_maintenance,
                    dream_maintenance=dream_maintenance,
                )
                return True
            except Exception:
                logger.warning("conversation-memory lifecycle hook failed", exc_info=True)
                return False

    async def run_compile(self, memory_dir: Path) -> bool:
        """Compile recent and durable memory for an explicit trigger."""
        async with self._lock_for(memory_dir):
            completed = await self._run_compilation_unlocked(memory_dir)
            if completed:
                self._mark_completed("compile", memory_dir)
            return completed

    async def run_dream(self, memory_dir: Path) -> bool:
        """Run Dream maintenance for an explicit trigger."""
        async with self._lock_for(memory_dir):
            completed = await self._run_dream_unlocked(memory_dir)
            if completed:
                self._mark_completed("dream", memory_dir)
            return completed

    async def run_idle_maintenance(self, memory_dir: Path) -> bool:
        """Retry only maintenance that was due or previously left pending."""
        async with self._lock_for(memory_dir):
            compiled = True
            dreamed = True
            if self._is_pending("compile", memory_dir):
                compiled = await self._run_compilation_unlocked(memory_dir)
                if compiled:
                    self._mark_completed("compile", memory_dir)
            if self._is_pending("dream", memory_dir):
                dreamed = await self._run_dream_unlocked(memory_dir)
                if dreamed:
                    self._mark_completed("dream", memory_dir)
            return compiled and dreamed

    async def _run_compilation_unlocked(self, memory_dir: Path) -> bool:
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
                ),
                timeout=_MEMORY_MAINTENANCE_TIMEOUT_SECONDS,
            )
            result = report["results"]
            errors = report["errors"]
            if result.get("recent") and not result.get("durable"):
                logger.info("recent memory compiled; durable memory remains pending review")
            return not errors
        except Exception:
            logger.warning("conversation-memory compilation failed", exc_info=True)
            return False

    async def _schedule_compilation(self, memory_dir: Path) -> bool:
        self._mark_pending("compile", memory_dir)
        self._schedule_background("compile", memory_dir)
        return False

    async def _schedule_dream(self, memory_dir: Path) -> bool:
        self._mark_pending("dream", memory_dir)
        self._schedule_background("dream", memory_dir)
        return False

    def _schedule_background(self, kind: str, memory_dir: Path) -> None:
        key = (memory_dir.resolve(), kind)
        existing = self._background_tasks.get(key)
        if existing is not None and not existing.done():
            return

        runner = (
            self._run_background_compilation
            if kind == "compile"
            else self._run_background_dream
        )
        task = asyncio.create_task(runner(memory_dir))
        self._background_tasks[key] = task

        def finish(completed: asyncio.Task[None]) -> None:
            self._background_tasks.pop(key, None)
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("background memory %s failed", kind, exc_info=True)

        task.add_done_callback(finish)

    async def _run_background_compilation(self, memory_dir: Path) -> None:
        async with self._lock_for(memory_dir):
            if await self._run_compilation_unlocked(memory_dir):
                self._mark_completed("compile", memory_dir)

    async def _run_background_dream(self, memory_dir: Path) -> None:
        async with self._lock_for(memory_dir):
            if await self._run_dream_unlocked(memory_dir):
                self._mark_completed("dream", memory_dir)

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
        try:
            from engine.memory.dream import dream_report_completed, run_dream

            report = await run_dream(memory_dir, self.llm, reviewer=self.reviewer)
            if not dream_report_completed(report):
                reason = "; ".join(report.errors) if report.errors else report.skipped
                logger.warning("conversation-memory Dream did not complete: %s", reason)
                return False
            return True
        except Exception:
            logger.warning("conversation-memory Dream consolidation failed", exc_info=True)
            return False

    @classmethod
    def _lock_for(cls, memory_dir: Path) -> asyncio.Lock:
        key = memory_dir.resolve()
        lock = cls._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            cls._locks[key] = lock
        return lock

    @staticmethod
    def _pending_path(kind: str, memory_dir: Path) -> Path:
        if kind == "compile":
            return memory_dir / _COMPILE_PENDING_FILE
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
        cls._clear_pending(kind, memory_dir)
        atomic_write_text(memory_dir / f".{kind}_counter", "0")

    @classmethod
    def _is_pending(cls, kind: str, memory_dir: Path) -> bool:
        if cls._pending_path(kind, memory_dir).is_file():
            return True
        try:
            if kind == "compile":
                from engine.memory.store import _COMPILE_INTERVAL

                threshold = _COMPILE_INTERVAL
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
