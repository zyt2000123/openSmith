from __future__ import annotations

import asyncio


def test_runner_owns_batch_and_commits_results_before_notes():
    from engine.execution.react.tool_round import ToolRoundRunner, ToolRoundResult
    from engine.llm.contracts import ToolCallData
    from engine.safety.tool_policy import ToolPolicy
    from engine.tool.registry import ToolRegistry
    async def run():
        registry = ToolRegistry()
        registry.register('get_current_time', 'Clock', {}, lambda: '12:00')
        registry.register('read', 'Read', {}, lambda: 'hello')
        runner = ToolRoundRunner(registry, ToolPolicy(None))
        events = [event async for event in runner.run([
            ToolCallData('clock', 'get_current_time', {}), ToolCallData('read', 'read', {})
        ])]
        result = events[-1]
        assert isinstance(result, ToolRoundResult)
        assert [m['role'] for m in result.messages] == ['tool', 'tool', 'user']
        assert [m['tool_call_id'] for m in result.messages[:2]] == ['clock', 'read']
        assert result.had_success and result.had_operational_success
        assert not result.terminal
        again = [e async for e in runner.run([ToolCallData('next', 'read', {})])]
        assert len(again[-1].messages) == 1
        assert len(result.messages) == 3
    asyncio.run(run())


def test_cancellation_does_not_execute_remaining_calls_and_releases_runner():
    from engine.execution.react.tool_round import ToolRoundRunner, ToolRoundResult
    from engine.llm.contracts import ToolCallData
    from engine.safety.tool_policy import ToolPolicy
    from engine.tool.registry import ToolRegistry
    async def run():
        registry = ToolRegistry()
        started = asyncio.Event()
        cancelled = asyncio.Event()
        touched = []
        async def suspended():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        registry.register('suspended', 'Wait', {}, suspended)
        registry.register('later', 'Effect', {}, lambda: touched.append(True))
        runner = ToolRoundRunner(registry, ToolPolicy(None))
        async def consume():
            return [e async for e in runner.run([
                ToolCallData('one', 'suspended', {}), ToolCallData('two', 'later', {})
            ])]
        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert cancelled.is_set() and not touched
        events = [e async for e in runner.run([])]
        assert isinstance(events[-1], ToolRoundResult)
    asyncio.run(run())


def test_lazy_schemas_preserve_previous_request_snapshot():
    from engine.execution.react.tool_round import ToolRoundRunner
    from engine.llm.contracts import ToolCallData
    from engine.safety.tool_policy import ToolPolicy
    from engine.tool.registry import ToolRegistry
    async def run():
        registry = ToolRegistry(lazy_tool_schemas=True)
        registry.register('read', 'Read', {}, lambda: 'hello')
        runner = ToolRoundRunner(registry, ToolPolicy(None))
        original = runner.tools
        assert [s['function']['name'] for s in original] == ['get_tool_schema']
        events = [e async for e in runner.run([ToolCallData('load', 'get_tool_schema', {'name': 'read'})])]
        assert events[-1].had_success and not events[-1].had_operational_success
        assert len(original) == 1
        assert len(runner.tools) == 2
    asyncio.run(run())


def test_closing_model_stream_closes_its_active_tool_round(monkeypatch):
    import importlib
    from engine.execution.events import EventType
    from engine.execution.react.tool_round import ToolRoundRunner
    from engine.llm.contracts import ChatResponse, ToolCallData
    from engine.safety.tool_policy import ToolPolicy
    from engine.tool.registry import ToolRegistry
    loop_module = importlib.import_module('engine.execution.react.react_loop')
    async def run():
        touched = []
        registry = ToolRegistry()
        registry.register('read', 'Read', {}, lambda: touched.append(True))
        runner = ToolRoundRunner(registry, ToolPolicy(None))
        monkeypatch.setattr(loop_module, 'ToolRoundRunner', lambda *args: runner)
        class Model:
            async def chat(self, messages, **kwargs):
                return ChatResponse(tool_calls=[ToolCallData('one', 'read', {})])
        stream = loop_module.react_event_loop(Model(), [{'role': 'user', 'content': 'read'}], registry)
        async for event in stream:
            if event.type == EventType.TOOL_CALL_START:
                break
        await stream.aclose()
        assert not touched
        # Closing the parent must synchronously release the nested iterator.
        assert len([e async for e in runner.run([])]) == 1
    asyncio.run(run())
