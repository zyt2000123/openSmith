from __future__ import annotations

import asyncio
import time

from engine.execution.hooks import HookManager, HookType


class AsyncCallableHandler:
    """Handler whose hook is an object with an async __call__ (not a
    coroutine function) — regression for un-awaited coroutine drops."""

    def __init__(self) -> None:
        class _Hook:
            called = False

            async def __call__(self, value):
                _Hook.called = True
                return value + "-modified"

        self.system_prompt = _Hook()


def test_series_last_awaits_async_callable_objects():
    manager = HookManager()
    handler = AsyncCallableHandler()
    manager.register(handler)

    result = asyncio.run(
        manager.apply("system_prompt", HookType.SERIES_LAST, initial="base")
    )

    assert result == "base-modified"
    assert handler.system_prompt.called


def test_parallel_collects_results_and_drops_failures():
    class Good:
        async def tools(self):
            return {"name": "good"}

    class Bad:
        async def tools(self):
            raise RuntimeError("boom")

    manager = HookManager()
    manager.register(Good())
    manager.register(Bad())

    result = asyncio.run(manager.apply("tools", HookType.PARALLEL))

    assert result == [{"name": "good"}]


def test_parallel_hook_timeout_is_reported_to_runtime_callers():
    class Slow:
        def tools(self):
            time.sleep(0.1)
            return {"name": "late"}

    async def run() -> None:
        manager = HookManager(timeout_seconds=0.01)
        manager.register(Slow())
        result = await manager.apply(
            "tools",
            HookType.PARALLEL,
            include_failures=True,
        )
        assert result == [False]

    asyncio.run(run())


def test_series_merge_infers_list_shape_without_an_initial_value() -> None:
    class First:
        def tools(self):
            return [{"name": "first"}]

    class Second:
        def tools(self):
            return {"name": "second"}

    manager = HookManager()
    manager.register(First())
    manager.register(Second())

    result = asyncio.run(manager.apply("tools", HookType.SERIES_MERGE))

    assert result == [{"name": "first"}, {"name": "second"}]


def test_series_merge_dict_result_drops_non_dict_partials() -> None:
    """A dict accumulator cannot merge a list partial; the partial must be
    dropped with a warning rather than silently corrupting the result."""
    class DictHook:
        def tools(self):
            return {"alpha": 1}

    class ListHook:
        def tools(self):
            return ["not-a-dict"]

    manager = HookManager()
    manager.register(DictHook())
    manager.register(ListHook())

    result = asyncio.run(manager.apply("tools", HookType.SERIES_MERGE))

    assert result == {"alpha": 1}
