"""Engine-extension hooks: the ``HookManager`` dispatch system.

These are *engine extension* hooks — intercepting prompt assembly, memory
lifecycle ticks, and after-turn persistence — as opposed to the tool-lifecycle
hooks in :mod:`engine.execution.hooks.tool` (PreToolHook / PostToolHook /
StopHook).  Both systems share the single import path
``engine.execution.hooks``.

Hooks let registered handlers intercept and modify behavior at key points:
- system_prompt: modify prompt before LLM call (SERIES_LAST)
- tool_use: before tool execution (SERIES)
- tool_result: post-process tool output (SERIES_LAST)
- tools: inject additional tools (SERIES_MERGE)
- after_turn: post-process conversation after each turn (SERIES_LAST)
- stop: after loop ends (SERIES)
- memory_*_tick / memory_after_turn_*: memory maintenance lifecycle (PARALLEL)
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)
DEFAULT_HOOK_TIMEOUT_SECONDS = 30.0


class HookType(Enum):
    FIRST = "first"
    SERIES = "series"
    SERIES_MERGE = "series_merge"
    SERIES_LAST = "series_last"
    PARALLEL = "parallel"


class HookManager:
    def __init__(self, *, timeout_seconds: float = DEFAULT_HOOK_TIMEOUT_SECONDS) -> None:
        if timeout_seconds <= 0:
            raise ValueError("hook timeout must be positive")
        self._handlers: list[Any] = []
        self._timeout_seconds = timeout_seconds

    def register(self, handler: Any) -> None:
        self._handlers.append(handler)
        self._handlers.sort(
            key=lambda h: {"pre": 0, "": 1, "post": 2}.get(
                getattr(h, "enforce", ""), 1
            )
        )

    def unregister(self, handler: Any) -> None:
        """Remove one previously registered handler, if present."""
        try:
            self._handlers.remove(handler)
        except ValueError:
            pass

    def is_registered(self, handler: Any) -> bool:
        """Return whether a handler is currently registered."""
        return handler in self._handlers

    def _get_handlers(self, hook: str) -> list[Callable]:
        return [
            fn
            for h in self._handlers
            if (fn := getattr(h, hook, None)) is not None and callable(fn)
        ]

    async def apply(
        self,
        hook: str,
        hook_type: HookType = HookType.SERIES,
        *,
        args: tuple = (),
        initial: Any = None,
        include_failures: bool = False,
    ) -> Any:
        handlers = self._get_handlers(hook)
        if not handlers:
            return initial

        if hook_type == HookType.FIRST:
            for fn in handlers:
                try:
                    result = await _call_with_timeout(fn, self._timeout_seconds, *args)
                    if result is not None:
                        return result
                except Exception:
                    logger.debug("hook %s.FIRST error", hook, exc_info=True)
            return None

        if hook_type == HookType.SERIES:
            for fn in handlers:
                try:
                    await _call_with_timeout(fn, self._timeout_seconds, *args)
                except Exception:
                    logger.debug("hook %s.SERIES error", hook, exc_info=True)
            return None

        if hook_type == HookType.SERIES_LAST:
            result = initial
            for fn in handlers:
                try:
                    result = await _call_with_timeout(fn, self._timeout_seconds, result, *args)
                except Exception:
                    logger.debug("hook %s.SERIES_LAST error", hook, exc_info=True)
            return result

        if hook_type == HookType.SERIES_MERGE:
            result = initial
            for fn in handlers:
                try:
                    partial = await _call_with_timeout(fn, self._timeout_seconds, *args)
                    if partial is None:
                        continue
                    if result is None:
                        result = [] if isinstance(partial, list) else {}
                    if isinstance(result, list):
                        result = result + (partial if isinstance(partial, list) else [partial])
                    elif isinstance(result, dict):
                        if isinstance(partial, dict):
                            merged = dict(result)
                            merged.update(partial)
                            result = merged
                        else:
                            # A non-dict partial cannot merge into a dict
                            # accumulator.  Log it instead of silently
                            # dropping a handler's contribution.
                            logger.warning(
                                "hook %s.SERIES_MERGE dropped a %s partial "
                                "that cannot merge into a dict result",
                                hook,
                                type(partial).__name__,
                            )
                except Exception:
                    logger.debug("hook %s.SERIES_MERGE error", hook, exc_info=True)
            return result

        if hook_type == HookType.PARALLEL:
            results = await asyncio.gather(
                *[_call_with_timeout(fn, self._timeout_seconds, *args) for fn in handlers],
                return_exceptions=True,
            )
            collected: list[Any] = []
            for r in results:
                if isinstance(r, BaseException):
                    logger.debug("hook %s.PARALLEL error", hook, exc_info=r)
                    if include_failures:
                        collected.append(False)
                elif r is not None:
                    collected.append(r)
            return collected

        return initial


async def _call(fn: Callable, *args: Any) -> Any:
    # Await by result, not by introspection: iscoroutinefunction misses
    # async callables such as objects with an async __call__, which would
    # silently return an un-awaited coroutine.
    is_async_callable = inspect.iscoroutinefunction(fn) or inspect.iscoroutinefunction(
        getattr(fn, "__call__", None)
    )
    result = fn(*args) if is_async_callable else await asyncio.to_thread(fn, *args)
    if inspect.isawaitable(result):
        return await result
    return result


async def _call_with_timeout(fn: Callable, timeout_seconds: float, *args: Any) -> Any:
    return await asyncio.wait_for(_call(fn, *args), timeout=timeout_seconds)


__all__ = (
    "DEFAULT_HOOK_TIMEOUT_SECONDS",
    "HookManager",
    "HookType",
)
