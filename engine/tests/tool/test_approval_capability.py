from __future__ import annotations

import asyncio
from pathlib import Path

from engine.safety.approval import ApprovalScope
from engine.safety.tool_guard import ToolGuard
from engine.tool.interface import ToolCall
from engine.tool.registry import ToolRegistry


class _ScopeAwareSandbox:
    name = "sandbox"

    def __init__(self, scope: ApprovalScope | None = None) -> None:
        self.scope = scope

    def with_approval_scope(self, scope: ApprovalScope) -> _ScopeAwareSandbox:
        return _ScopeAwareSandbox(scope)

    async def run_command(self, *args, **kwargs):  # pragma: no cover - provider only inspects scope.
        raise AssertionError("not used by this contract test")


def test_registry_binds_dynamic_host_scope_to_the_exact_approved_call(tmp_path: Path) -> None:
    async def run():
        registry = ToolRegistry()

        async def shell(*, command: str, environment) -> str:
            assert environment.scope is not None
            return f"{environment.scope.kind}:{environment.scope.target}"

        registry.register(
            "shell",
            "Run a command",
            {"type": "object", "properties": {"command": {"type": "string"}}},
            shell,
            opaque_command=True,
            permission_level="execute",
            approval_policy="always",
            side_effect="external",
            execution_environment="sandbox",
        )
        guard = ToolGuard(tmp_path / "missing-rules.json", tool_registry=registry.definitions())
        registry.bind_tool_guard(guard)
        registry.bind_execution_environment(_ScopeAwareSandbox())
        call = ToolCall(id="approved-call", name="shell", arguments={"command": "pwd"})
        scope = ApprovalScope.host_command("pwd")

        with registry.authorize_execution(
            call,
            approval_id="approval-1",
            approval_scope=ApprovalScope.host_command("id"),
        ):
            mismatched_scope = await registry.execute(call)

        with registry.authorize_execution(
            call,
            approval_id="approval-1",
            approval_scope=scope,
        ):
            approved = await registry.execute(call)

        changed = ToolCall(id="approved-call", name="shell", arguments={"command": "id"})
        with registry.authorize_execution(
            call,
            approval_id="approval-1",
            approval_scope=scope,
        ):
            replay = await registry.execute(changed)
        return mismatched_scope, approved, replay

    mismatched_scope, approved, replay = asyncio.run(run())

    assert mismatched_scope.is_error
    assert "approval" in mismatched_scope.content.lower()
    assert approved.content == "host_command:pwd"
    assert not approved.is_error
    assert replay.is_error
    assert "approval" in replay.content.lower()
