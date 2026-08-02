"""Risk-tier triage: approvals with different risk must be distinguishable.

``high_risk`` used to be computed by the guard but never changed any behavior.
Today the tier is carried on the guard result, the policy decision, the
approval request, the presentation, and the emitted events — and the registry
only caches session whitelists for the lowest approval tier, so high/critical
approvals must be re-granted each time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.execution.events import EventType
from engine.execution.react.react_loop import react_event_loop
from engine.llm.client import ChatResponse
from engine.llm.contracts import ToolCallData
from engine.safety.approval import (
    ApprovalRequest,
    ApprovalScope,
    build_approval_presentation,
)
from engine.safety.risk import RiskTier, risk_for_approval
from engine.safety.tool_guard import PermissionLevel, ToolGuard
from engine.tool.interface import ToolCall
from engine.tool.registry import ToolRegistry


def _guard(tmp_path: Path, *, rules: str = "[]", allowed_dirs=None) -> ToolGuard:
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(rules, encoding="utf-8")
    return ToolGuard(rules_path, allowed_dirs=allowed_dirs)


def _register_read_file(registry: ToolRegistry, guard: ToolGuard) -> None:
    def read_file_impl(path: str) -> str:
        return "content"
    registry.register(
        "read_file",
        "Read a file",
        {"properties": {"path": {"type": "string"}}, "required": ["path"]},
        read_file_impl,
        path_args=("path",),
        is_write_tool=False,
    )
    guard.bind_definitions(registry.definitions())


def test_risk_for_approval_tiers() -> None:
    assert risk_for_approval(level="write") is RiskTier.ELEVATED
    assert risk_for_approval(level="write", high_risk=True) is RiskTier.HIGH
    assert risk_for_approval(level="write", network_access=True) is RiskTier.HIGH
    assert risk_for_approval(level="destructive") is RiskTier.CRITICAL
    assert risk_for_approval(level="write", rule_hit=True) is RiskTier.CRITICAL


def test_risk_tier_ordering() -> None:
    assert RiskTier.max() is RiskTier.ROUTINE
    assert RiskTier.max(RiskTier.ELEVATED, RiskTier.HIGH) is RiskTier.HIGH
    assert RiskTier.max(RiskTier.CRITICAL, RiskTier.HIGH) is RiskTier.CRITICAL
    assert RiskTier.HIGH.weight > RiskTier.ELEVATED.weight


def test_routine_read_is_not_approval_gated(tmp_path: Path) -> None:
    registry = ToolRegistry()
    guard = _guard(tmp_path, allowed_dirs=[tmp_path])
    _register_read_file(registry, guard)
    target = tmp_path / "plain.txt"
    target.write_text("x", encoding="utf-8")

    decision = guard.check(ToolCall(id="c", name="read_file", arguments={"path": str(target)}))
    assert decision.allowed
    assert decision.risk is RiskTier.ROUTINE


def test_sensitive_read_requires_high_tier_approval(tmp_path: Path) -> None:
    registry = ToolRegistry()
    guard = _guard(tmp_path, allowed_dirs=[tmp_path])
    _register_read_file(registry, guard)
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET=1", encoding="utf-8")

    decision = guard.check(ToolCall(id="c", name="read_file", arguments={"path": str(env_file)}))
    assert decision.approval_required
    assert decision.risk is RiskTier.HIGH


def test_outside_workspace_write_is_elevated(tmp_path: Path) -> None:
    working_dir = tmp_path / "project"
    working_dir.mkdir()
    external = tmp_path / "outside"
    external.mkdir()
    registry = ToolRegistry()

    def write_file_impl(path: str, content: str) -> str:
        return "written"
    registry.register(
        "write_file",
        "Write",
        {"properties": {"path": {"type": "string"}}, "required": ["path"]},
        write_file_impl,
        path_args=("path",),
        is_write_tool=True,
        permission_level="write",
        approval_policy="policy",
        side_effect="write",
    )
    guard = _guard(tmp_path)
    guard.set_working_directory(working_dir)
    guard.bind_definitions(registry.definitions())

    decision = guard.check(
        ToolCall(id="c", name="write_file", arguments={"path": str(external / "x.txt"), "content": "x"})
    )
    assert decision.approval_required
    assert decision.boundary_block
    assert decision.risk is RiskTier.ELEVATED


def test_network_access_tool_is_high(tmp_path: Path) -> None:
    registry = ToolRegistry()

    async def fetch_impl(url: str) -> str:
        return "fetched"
    registry.register(
        "web_fetch",
        "Fetch",
        {"properties": {"url": {"type": "string"}}, "required": ["url"]},
        fetch_impl,
        network_access=True,
        approval_policy="always",
    )
    guard = _guard(tmp_path)
    guard.bind_definitions(registry.definitions())

    decision = guard.check(ToolCall(id="c", name="web_fetch", arguments={"url": "https://example.com"}))
    assert decision.approval_required
    assert decision.risk is RiskTier.HIGH
    assert decision.approval_scope is not None
    assert decision.approval_scope.kind == "network"


def test_destructive_level_tool_is_critical(tmp_path: Path) -> None:
    registry = ToolRegistry()

    async def destroy_impl() -> str:
        return "done"
    registry.register(
        "destroy",
        "Destroy",
        {},
        destroy_impl,
        permission_level="destructive",
        approval_policy="always",
        side_effect="destructive",
    )
    guard = _guard(tmp_path)
    guard.bind_definitions(registry.definitions())

    decision = guard.check(ToolCall(id="c", name="destroy", arguments={}))
    assert decision.approval_required
    assert decision.risk is RiskTier.CRITICAL


def test_dangerous_rule_hit_is_critical(tmp_path: Path) -> None:
    rules = '[{"id": "rm-rf", "patterns": ["rm\\\\s+-rf"], "reason": "dangerous rm"}]'
    registry = ToolRegistry()

    async def run_cmd(command: str) -> str:
        return "ran"
    registry.register(
        "run_command",
        "Run",
        {"properties": {"command": {"type": "string"}}, "required": ["command"]},
        run_cmd,
        approval_policy="always",
    )
    guard = _guard(tmp_path, rules=rules)
    guard.bind_definitions(registry.definitions())

    decision = guard.check(
        ToolCall(id="c", name="run_command", arguments={"command": "rm -rf /tmp/x"})
    )
    assert decision.approval_required
    assert decision.risk is RiskTier.CRITICAL


def test_approval_request_and_presentation_carry_risk() -> None:
    request = ApprovalRequest(
        approval_id="a1",
        run_id="r1",
        tool_name="read_file",
        level="read",
        reason="high-risk read",
        arguments_summary={"path": "/tmp/.env"},
        scope=ApprovalScope.path("/tmp/.env", writing=False, high_risk=True),
        risk=RiskTier.HIGH,
    )
    payload = request.to_dict()
    assert payload["risk"] == "high"

    presentation = build_approval_presentation(
        "read_file",
        "read",
        "high-risk read",
        {"path": "/tmp/.env"},
        scope=request.scope,
    )
    assert "risk" not in presentation.to_dict()  # optional; set explicitly by callers
    presentation_with_risk = presentation.__class__(
        title=presentation.title,
        summary=presentation.summary,
        details=presentation.details,
        reason=presentation.reason,
        risk="critical",
    )
    assert presentation_with_risk.to_dict()["risk"] == "critical"


def test_approval_event_carries_risk(tmp_path: Path) -> None:
    """The TOOL_CALL_RESULT that pauses for approval must expose the tier."""
    import asyncio

    registry = ToolRegistry()

    async def write_file(path: str, content: str) -> str:
        Path(path).write_text(content, encoding="utf-8")
        return "written"
    registry.register(
        "write_file",
        "Write",
        {"properties": {"path": {"type": "string"}}, "required": ["path"]},
        write_file,
        path_args=("path",),
        permission_level="write",
        approval_policy="policy",
        side_effect="write",
    )
    guard = _guard(tmp_path, allowed_dirs=[tmp_path])
    guard.bind_definitions(registry.definitions())

    class ApprovalLLM:
        def __init__(self):
            self.calls = 0
        async def chat(self, messages, tools=None, prefix_cache_key=None):
            self.calls += 1
            if self.calls == 1:
                return ChatResponse(
                    tool_calls=[ToolCallData(id="t", name="write_file", arguments={"path": str(tmp_path / "x.txt"), "content": "x"})]
                )
            return ChatResponse(text="done")

    from engine.safety.approval import APPROVAL_BROKER, use_approval_context

    async def run():
        events = []

        async def consume():
            async for event in react_event_loop(
                ApprovalLLM(),
                [{"role": "user", "content": "write"}],
                registry,
                guard,
                max_iters=3,
            ):
                events.append(event)
                if event.type is EventType.TOOL_CALL_RESULT and event.data.get("approval_required"):
                    APPROVAL_BROKER.resolve("run-risk", str(event.data["approval_id"]), True)

        with use_approval_context(APPROVAL_BROKER, "run-risk"):
            await consume()
        return events

    events = asyncio.run(run())
    approval_events = [
        event for event in events
        if event.type is EventType.TOOL_CALL_RESULT and event.data.get("approval_required")
    ]
    assert len(approval_events) == 1
    assert approval_events[0].data["risk"] == "elevated"
