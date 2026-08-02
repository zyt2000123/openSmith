"""Tests for Hook system integration"""

from __future__ import annotations

import pytest

from engine.execution.tool_hooks import HookLoader, HookRegistry, PreToolHook


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
        # Block Edit tool
        if tool_name == "Edit":
            return False, "Edit blocked by mock hook"
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
    allowed, reason = await registry.run_pre_hooks("Read", {"file_path": "test.py"})

    assert allowed is True
    assert reason is None


@pytest.mark.asyncio
async def test_pre_hook_blocks_tool():
    """Test Pre Hook blocks tool execution"""
    registry = HookRegistry()
    hook = MockPreHook()
    registry.register_pre_hook(hook)

    # Edit tool should be blocked
    allowed, reason = await registry.run_pre_hooks(
        "Edit", {"file_path": "test.py", "old_string": "a", "new_string": "b"}
    )

    assert allowed is False
    assert reason is not None
    assert "Edit blocked" in reason


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
