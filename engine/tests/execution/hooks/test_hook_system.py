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
