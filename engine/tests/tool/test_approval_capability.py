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


def test_registry_binds_sandbox_command_and_network_scopes_without_escalation(
    tmp_path: Path,
) -> None:
    async def run():
        registry = ToolRegistry()

        async def shell(*, command: str, network: bool = False, environment) -> str:
            assert environment.scope is not None
            return f"{environment.scope.kind}:{environment.scope.target}:net={network}"

        registry.register(
            "shell",
            "Run a command",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "network": {"type": "boolean", "default": False},
                },
            },
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
        scope = ApprovalScope.sandbox_command("pwd")
        net_call = ToolCall(
            id="approved-net", name="shell", arguments={"command": "curl x", "network": True}
        )
        net_scope = ApprovalScope.sandbox_network("curl x", high_risk=True)

        # Mismatched scope (wrong command or wrong scope kind) is rejected
        with registry.authorize_execution(
            call,
            approval_id="approval-1",
            approval_scope=ApprovalScope.sandbox_command("id"),
        ):
            mismatched_scope = await registry.execute(call)

        # Cannot replay a sandbox_command approval for a network=True call
        with registry.authorize_execution(
            net_call,
            approval_id="approval-1",
            approval_scope=ApprovalScope.sandbox_command("curl x"),
        ):
            wrong_kind = await registry.execute(net_call)

        with registry.authorize_execution(
            call,
            approval_id="approval-1",
            approval_scope=scope,
        ):
            approved = await registry.execute(call)

        with registry.authorize_execution(
            net_call,
            approval_id="approval-2",
            approval_scope=net_scope,
        ):
            approved_net = await registry.execute(net_call)

        changed = ToolCall(id="approved-call", name="shell", arguments={"command": "id"})
        with registry.authorize_execution(
            call,
            approval_id="approval-1",
            approval_scope=scope,
        ):
            replay = await registry.execute(changed)

        # Degraded host sandbox elevates to host_command scope and CRITICAL risk
        guard.set_sandbox_degraded(True)
        degraded_decision = guard.check(call, audit=False)
        return (
            mismatched_scope,
            wrong_kind,
            approved,
            approved_net,
            replay,
            degraded_decision,
        )

    (
        mismatched_scope,
        wrong_kind,
        approved,
        approved_net,
        replay,
        degraded_decision,
    ) = asyncio.run(run())

    assert mismatched_scope.is_error
    assert "approval" in mismatched_scope.content.lower()
    assert wrong_kind.is_error
    assert "approval" in wrong_kind.content.lower()
    assert approved.content == "sandbox_command:pwd:net=False"
    assert not approved.is_error
    assert approved.metadata["network_used"] is False
    assert approved_net.content == "sandbox_network:curl x:net=True"
    assert not approved_net.is_error
    assert approved_net.metadata["network_used"] is True
    assert replay.is_error
    assert "approval" in replay.content.lower()
    assert degraded_decision.approval_scope is not None
    assert degraded_decision.approval_scope.kind == "host_command"
    assert "[NO OS SANDBOX]" in degraded_decision.reason
