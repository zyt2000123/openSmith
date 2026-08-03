"""Tests for Hook system integration"""

from __future__ import annotations

import pytest

from engine.execution.hooks import HookLoader, HookRegistry, PreToolHook


class MockPreHook(PreToolHook):
    """Mock Pre Hook for testing"""

    @property
    def id(self) -> str:
        return "mock-pre-hook"

    @property
    def priority(self) -> int:
        return 10

    async def check(
        self, tool_name: str, tool_input: dict
    ) -> tuple[bool, str | None]:
        # Block edit_file tool
        if tool_name == "edit_file":
            return False, "edit_file blocked by mock hook"
        return True, None


def test_hook_registry_registration():
    """Test Hook registration"""
    registry = HookRegistry()
    hook = MockPreHook()

    registry.register_pre_hook(hook)

    assert registry.get_pre_hook("mock-pre-hook") is hook


@pytest.mark.asyncio
async def test_pre_hook_allows_tool():
    """Test Pre Hook allows tool execution"""
    registry = HookRegistry()
    hook = MockPreHook()
    registry.register_pre_hook(hook)

    # Read tool should be allowed
    allowed, reason = await registry.run_pre_hooks("read_file", {"path": "test.py"})

    assert allowed is True
    assert reason is None


@pytest.mark.asyncio
async def test_pre_hook_blocks_tool():
    """Test Pre Hook blocks tool execution"""
    registry = HookRegistry()
    hook = MockPreHook()
    registry.register_pre_hook(hook)

    # edit_file tool should be blocked
    allowed, reason = await registry.run_pre_hooks(
        "edit_file", {"path": "test.py", "old_string": "a", "new_string": "b"}
    )

    assert allowed is False
    assert reason is not None
    assert "edit_file blocked" in reason


@pytest.mark.asyncio
async def test_hook_priority_ordering():
    """Test hooks execute in priority order"""
    execution_order = []

    class HighPriorityHook(PreToolHook):
        @property
        def id(self) -> str:
            return "high"

        @property
        def priority(self) -> int:
            return 1

        async def check(self, tool_name: str, tool_input: dict) -> tuple[bool, str | None]:
            execution_order.append("high")
            return True, None

    class LowPriorityHook(PreToolHook):
        @property
        def id(self) -> str:
            return "low"

        @property
        def priority(self) -> int:
            return 10

        async def check(self, tool_name: str, tool_input: dict) -> tuple[bool, str | None]:
            execution_order.append("low")
            return True, None

    registry = HookRegistry()
    registry.register_pre_hook(LowPriorityHook())
    registry.register_pre_hook(HighPriorityHook())

    await registry.run_pre_hooks("Test", {})

    # High priority (1) should execute before low priority (10)
    assert execution_order == ["high", "low"]


def test_hook_registry_list():
    """Test listing registered hooks"""
    registry = HookRegistry()
    hook = MockPreHook()
    registry.register_pre_hook(hook)

    hooks_list = registry.list_registered_hooks()

    assert "pre" in hooks_list
    assert "mock-pre-hook" in hooks_list["pre"]


@pytest.mark.asyncio
async def test_builtin_config_protection_hook_fires_on_engine_tool_names(tmp_path):
    """Regression: the built-in config-protection hook must match the engine's
    real tool name (edit_file) and path argument key (path) — not the
    Claude-Code-style "Edit"/"file_path" it was written against.  With the old
    contract the top-priority safety hook never fired and configs could be
    edited freely."""
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(repo_root))
    from agents.smith.hooks.config_protection import ConfigProtectionHook

    hook = ConfigProtectionHook()
    registry = HookRegistry()
    registry.register_pre_hook(hook)

    # Editing a protected config through the real tool contract must be blocked.
    allowed, reason = await registry.run_pre_hooks(
        "edit_file",
        {"path": str(tmp_path / "pyproject.toml"), "old_string": "a", "new_string": "b"},
    )
    assert allowed is False
    assert reason is not None
    assert "Config file modification blocked" in reason

    # Editing ordinary source through the real contract must be allowed.
    allowed, reason = await registry.run_pre_hooks(
        "edit_file",
        {"path": str(tmp_path / "main.py"), "old_string": "a", "new_string": "b"},
    )
    assert allowed is True


def test_loader_injects_configured_priority_over_class_default(tmp_path):
    """YAML priority must take effect even when the hook class relies on the
    base-class default (100)."""
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(repo_root))
    from agents.smith.hooks.config_protection import ConfigProtectionHook

    loader = HookLoader()
    hook_def = {
        "id": "config-protection",
        "module": "agents/smith/hooks/config_protection.py",
        "class": "ConfigProtectionHook",
        "priority": 1,
    }
    hook = loader._load_pre_hook(hook_def, repo_root / "agents" / "smith")

    assert hook is not None
    assert hook.priority == 1


# ── Integration: the react_loop <-> hook_registry seam ──
#
# The tests above exercise HookRegistry in isolation.  Nothing covered the loop
# that calls it, which is how a denied Pre hook came to leave an assistant
# tool_calls entry with no paired tool result — malformed history that every
# provider rejects on the *next* request.  config-protection ships enabled, so
# any edit to pyproject.toml / tsconfig.json reached it.


class _DenyingPreHook(PreToolHook):
    @property
    def id(self) -> str:
        return "deny-everything"

    @property
    def priority(self) -> int:
        return 1

    async def check(self, tool_name, tool_input):
        return False, "config file modification blocked"


class _RecordingLLM:
    """Captures the conversation handed to each provider call."""

    stream = False

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []
        self._turn = 0

    async def chat(self, messages, tools=None, **kwargs):
        from engine.llm.client import ChatResponse
        from engine.llm.contracts import ToolCallData

        self.calls.append([dict(message) for message in messages])
        self._turn += 1
        if self._turn == 1:
            return ChatResponse(
                text="",
                tool_calls=[
                    ToolCallData(
                        id="call-1",
                        name="write_file",
                        arguments={"path": "pyproject.toml", "content": "x"},
                    )
                ],
            )
        return ChatResponse(text="done")


def _unanswered_tool_calls(conversation: list[dict]) -> list[str]:
    """Tool-call ids in the history with no matching tool result message."""
    answered = {
        message.get("tool_call_id")
        for message in conversation
        if message.get("role") == "tool"
    }
    requested: list[str] = []
    for message in conversation:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            requested.extend(call["id"] for call in message["tool_calls"])
    return [call_id for call_id in requested if call_id not in answered]


@pytest.mark.asyncio
async def test_pre_hook_denial_leaves_no_unanswered_tool_call() -> None:
    from engine.execution.events import EventType
    from engine.execution.react.react_loop import react_event_loop
    from engine.tool.registry import ToolRegistry

    registry = ToolRegistry()

    async def write_file(path: str, content: str) -> str:  # pragma: no cover
        raise AssertionError("the hook must block execution")

    registry.register(
        "write_file", "Write", {}, write_file,
        permission_level="write", side_effect="write",
    )
    hooks = HookRegistry()
    hooks.register_pre_hook(_DenyingPreHook())

    llm = _RecordingLLM()
    events = []
    async for event in react_event_loop(
        llm,
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "edit config"}],
        registry,
        None,
        max_iters=3,
        hook_registry=hooks,
    ):
        events.append(event)

    assert len(llm.calls) == 2, "the loop must continue after a hook denial"
    for conversation in llm.calls:
        assert _unanswered_tool_calls(conversation) == []

    blocked = [
        event for event in events
        if event.type is EventType.TOOL_CALL_RESULT and event.data.get("blocked")
    ]
    assert len(blocked) == 1
    assert "config file modification blocked" in blocked[0].data["reason"]
    # The denial reaches the model as an observation it can act on.
    denial = [
        message for message in llm.calls[1]
        if message.get("role") == "tool" and message.get("tool_call_id") == "call-1"
    ]
    assert denial and denial[0]["content"].startswith("[BLOCKED]")


@pytest.mark.asyncio
async def test_repeated_pre_hook_denial_stops_instead_of_burning_the_budget() -> None:
    """A hook denies deterministically, so retrying it cannot ever succeed."""
    from engine.execution.events import EventType
    from engine.execution.react.react_loop import react_event_loop
    from engine.llm.client import ChatResponse
    from engine.llm.contracts import ToolCallData
    from engine.tool.registry import ToolRegistry

    class _AlwaysRetriesLLM:
        stream = False

        def __init__(self) -> None:
            self.turns = 0

        async def chat(self, messages, tools=None, **kwargs):
            self.turns += 1
            return ChatResponse(
                text="",
                tool_calls=[
                    ToolCallData(
                        id=f"call-{self.turns}",
                        name="write_file",
                        arguments={"path": "pyproject.toml", "content": "x"},
                    )
                ],
            )

    registry = ToolRegistry()

    async def write_file(path: str, content: str) -> str:  # pragma: no cover
        raise AssertionError("the hook must block execution")

    registry.register(
        "write_file", "Write", {}, write_file,
        permission_level="write", side_effect="write",
    )
    hooks = HookRegistry()
    hooks.register_pre_hook(_DenyingPreHook())

    llm = _AlwaysRetriesLLM()
    reasons = [
        event.data.get("reason")
        async for event in react_event_loop(
            llm,
            [{"role": "user", "content": "edit config"}],
            registry,
            None,
            max_iters=60,
            hook_registry=hooks,
        )
        if event.type is EventType.INCOMPLETE
    ]

    assert reasons == ["identical_tool_error_loop"]
    assert llm.turns < 60, "the identical-error guard must stop the loop early"
