from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.execution.events import EventType, ExecutionEvent, raw_text_delta
from engine.execution.hooks import HookManager
from engine.execution.orchestration import lifecycle as lifecycle_module
from engine.execution.orchestration import preparation as preparation_module
from engine.execution.orchestration.agent_loop import run_agent_stream
from engine.execution.orchestration.lifecycle import (
    reply_events_with_runtime,
    reply_with_runtime,
    resume_stream_with_runtime,
    run_stream_with_runtime,
)
from engine.execution.orchestration.preparation import prepare_runtime
from engine.execution.orchestration.run_state import (
    RunStateStore,
    RunStatus,
    project_execution_event,
)
from engine.execution.orchestration.runtime import (
    EngineRequest,
    RuntimeContext,
    RuntimeServices,
)
from engine.execution.pipeline.backtrack import FailureLoopGuard
from engine.identity import IdentityCatalog
from engine.llm.client import ChatResponse, ToolCallData
from engine.llm.contracts import LLMResponseError, ProviderCapabilities
from engine.observability import RunObservation, RunSummaryStore, TraceStore
from engine.safety.approval import APPROVAL_BROKER
from engine.safety.tool_guard import AuditLog, ToolGuard
from engine.sandbox import CommandResult, MacOSSeatbeltEnvironment
from engine.skill.registry import SkillRegistry
from engine.tool.interface import ToolCall
from engine.tool.registry import ToolRegistry


class FakeLLM:
    def __init__(self) -> None:
        self.closed = False
        self.messages: list[dict] = []

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        prefix_cache_key: str | None = None,
    ) -> ChatResponse:
        self.messages = messages
        return ChatResponse(text="runtime reply")

    async def chat_stream(
        self,
        messages: list[dict],
    ):
        self.messages = messages
        yield "streamed reply"

    async def close(self) -> None:
        self.closed = True


class ToolCallingLLM(FakeLLM):
    def __init__(self) -> None:
        super().__init__()
        self.responses = [
            ChatResponse(tool_calls=[ToolCallData(id="call-1", name="test_tool", arguments={})]),
            ChatResponse(text="tool-assisted reply"),
        ]

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        prefix_cache_key: str | None = None,
    ) -> ChatResponse:
        self.messages = messages
        return self.responses.pop(0)


class ShellCallingLLM(FakeLLM):
    def __init__(self, command: str = "pwd") -> None:
        super().__init__()
        self.responses = [
            ChatResponse(
                tool_calls=[
                    ToolCallData(
                        id="shell-call-1",
                        name="shell",
                        arguments={"command": command},
                    )
                ]
            ),
            ChatResponse(text="sandbox complete"),
        ]

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        prefix_cache_key: str | None = None,
    ) -> ChatResponse:
        self.messages = messages
        return self.responses.pop(0)


class RetryingShellCallingLLM(FakeLLM):
    def __init__(self, command: str) -> None:
        super().__init__()
        self.responses = [
            ChatResponse(
                tool_calls=[
                    ToolCallData(
                        id="shell-call-preflight",
                        name="shell",
                        arguments={"command": command},
                    )
                ]
            ),
            ChatResponse(
                tool_calls=[
                    ToolCallData(
                        id="shell-call-approved",
                        name="shell",
                        arguments={"command": command},
                    )
                ]
            ),
            ChatResponse(text="sandbox write protection complete"),
        ]

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        prefix_cache_key: str | None = None,
    ) -> ChatResponse:
        self.messages = messages
        return self.responses.pop(0)


class ToolCallingMemoryLLM(ToolCallingLLM):
    def __init__(self) -> None:
        super().__init__()
        self.chat_calls: list[list[dict]] = []

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        prefix_cache_key: str | None = None,
    ) -> ChatResponse:
        self.chat_calls.append(messages)
        self.messages = messages
        if self.responses:
            return self.responses.pop(0)
        prompt = messages[-1]["content"]
        if "`memory/recent.md`" in prompt:
            return ChatResponse(text="""# Recent Working Memory

## Active Work
- **Runtime memory** — 状态：active；下一步：verify；更新：2026-07-13。

## Pending

## Recent Verified Outcomes
""")
        if "`memory/durable.md`" in prompt:
            return ChatResponse(text="""# Durable Project Memory

## Confirmed Facts
- **Runtime memory**: Tool-assisted turns enter the memory pipeline.

## Decisions

## Reusable Procedures

## Known Pitfalls
""")
        return ChatResponse(text="stable memory summary")


class PassReviewer(FakeLLM):
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        prefix_cache_key: str | None = None,
    ) -> ChatResponse:
        self.messages = messages
        return ChatResponse(
            text='{"pass": true, "hard_fail": [], "soft_fail": [], "feedback": ""}'
        )


class LlmGatePassReviewer(FakeLLM):
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        prefix_cache_key: str | None = None,
    ) -> ChatResponse:
        self.messages = messages
        return ChatResponse(text="PASS")


class CodingPipelineLLM(FakeLLM):
    def __init__(self) -> None:
        super().__init__()
        self.responses = [
            "需求：修复登录报错，目标是保证认证恢复正常。边界：不改动注册流程；约束：保持现有 API；风险：兼容旧 token。",
            "1. 检查 auth/login.py 的错误路径并确认现有契约。\n2. 修改 server/app/auth.py 的验证分支。\n3. 在 shell/src/login.tsx 补充回归测试。\n验证：执行 pytest tests/test_auth.py 确认结果。",
            "涉及文件 server/app/auth.py、shell/src/login.tsx 和 tests/test_auth.py。数据流：请求 -> 鉴权 -> 响应。依赖：复用现有 token 校验器。",
            "实现方案与计划一致：按第 1 步定位 auth/login.py，按第 2 步修改 server/app/auth.py，按第 3 步更新 shell/src/login.tsx；整体对齐，无偏差。",
            "执行 pytest tests/test_auth.py，结果 3 passed, 0 failed。",
        ]

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        prefix_cache_key: str | None = None,
    ) -> ChatResponse:
        self.messages = messages
        return ChatResponse(text=self.responses.pop(0))


def _write_profile(profile_dir: Path) -> None:
    profile_dir.mkdir(parents=True)
    for filename, content in {
        "role.md": "You are Smith.",
        "style.md": "Be clear.",
        "workflow.md": "Pick skills only when needed.",
        "toolbox.md": "Use tools deliberately.",
        "context.md": "Remember the user's preferences.",
        "config.yaml": "tools:\n  enabled: []\n",
    }.items():
        (profile_dir / filename).write_text(content, encoding="utf-8")


def _runtime(tmp_path: Path) -> tuple[RuntimeContext, RuntimeServices, FakeLLM]:
    profile_dir = tmp_path / "profile"
    agents_dir = tmp_path / "agents"
    _write_profile(profile_dir)
    (agents_dir / "tools").mkdir(parents=True)
    (agents_dir / "skills").mkdir(parents=True)
    identities_dir = agents_dir / "identities"
    identities_dir.mkdir()
    (identities_dir / "smith.yaml").write_text(
        """
schema: agentsmith.identity/v1
id: smith
name: Smith
default: true
routes: []
""".strip(),
        encoding="utf-8",
    )

    llm = FakeLLM()
    runtime = RuntimeContext(
        agent_id="smith",
        agent_name="Smith",
        profile_dir=profile_dir,
        agents_dir=agents_dir,
        session_id="sess-1",
        identity_catalog=IdentityCatalog.load(identities_dir),
    )
    services = RuntimeServices(
        llm=llm,  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
        skill_registry=SkillRegistry(),
        observation_factory=RunObservation.start,
    )
    return runtime, services, llm


def _register_successful_test_tool(runtime: RuntimeContext, services: RuntimeServices) -> None:
    (runtime.profile_dir / "config.yaml").write_text(
        "tools:\n  enabled: [test_tool]\n",
        encoding="utf-8",
    )

    async def execute() -> str:
        return "verified tool result"

    services.tool_registry.register(
        "test_tool",
        "Test-only successful tool.",
        {"type": "object", "properties": {}},
        execute,
    )


def test_prepare_runtime_scopes_tool_paths_to_the_request_working_dir(tmp_path: Path) -> None:
    async def run() -> tuple[ToolCall, ToolCall, ToolCall, ToolGuard]:
        runtime, services, _ = _runtime(tmp_path)
        project_dir = tmp_path / "OpenAI_project"
        project_dir.mkdir()
        services.tool_registry.register(
            "write_file",
            "",
            {"type": "object", "properties": {"path": {"type": "string"}}},
            lambda **_kwargs: "OK",
            path_args=("path",),
            is_write_tool=True,
            permission_level="write",
            approval_policy="policy",
            side_effect="write",
        )
        services.tool_registry.register(
            "shell",
            "",
            {"type": "object", "properties": {"cwd": {"type": "string"}}},
            lambda **_kwargs: "OK",
            path_args=("cwd",),
            opaque_command=True,
            permission_level="execute",
            approval_policy="always",
            side_effect="external",
        )
        guard = ToolGuard(tmp_path / "missing-rules.json")
        services.tool_guard = guard

        await prepare_runtime(
            EngineRequest(message="Inspect the project", working_dir=str(project_dir)),
            runtime,
            services,
        )
        write = services.tool_registry.normalize_call(
            ToolCall(
                id="write",
                name="write_file",
                arguments={"path": "app/main.py", "content": "x"},
            )
        )
        shell = services.tool_registry.normalize_call(
            ToolCall(id="shell", name="shell", arguments={"command": "test -d . 2>/dev/null"})
        )
        escaped = services.tool_registry.normalize_call(
            ToolCall(id="escaped", name="write_file", arguments={"path": "../outside.txt", "content": "x"})
        )
        return write, shell, escaped, guard

    write, shell, escaped, guard = asyncio.run(run())

    assert write.arguments["path"] == str(
        (tmp_path / "OpenAI_project" / "app" / "main.py").resolve()
    )
    assert shell.arguments["cwd"] == str((tmp_path / "OpenAI_project").resolve())
    assert guard.check(write).allowed
    assert not guard.check(shell).allowed
    assert not guard.check(escaped).allowed


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_prepare_runtime_routes_sandbox_tools_to_macos_seatbelt(tmp_path: Path) -> None:
    async def run() -> tuple[Path, object]:
        runtime, services, _ = _runtime(tmp_path)
        project_dir = tmp_path / "sandboxed_project"
        project_dir.mkdir()
        (runtime.profile_dir / "config.yaml").write_text(
            "tools:\n  enabled: [sandbox_probe]\n", encoding="utf-8"
        )
        (runtime.agents_dir / "tools" / "sandbox_probe.py").write_text(
            "TOOL_META = {\n"
            "    'name': 'sandbox_probe',\n"
            "    'parameters': {'type': 'object', 'properties': {}},\n"
            "    'execution_environment': 'sandbox',\n"
            "}\n"
            "async def execute(*, environment=None):\n"
            "    return f'env={environment.name}'\n",
            encoding="utf-8",
        )
        await prepare_runtime(
            EngineRequest(message="Inspect the project", working_dir=str(project_dir)),
            runtime,
            services,
        )
        result = await services.tool_registry.execute(
            ToolCall(id="sandbox", name="sandbox_probe", arguments={})
        )
        return project_dir, result

    project_dir, result = asyncio.run(run())

    assert not result.is_error, result.content
    assert result.content == "env=sandbox"


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_prepare_runtime_executes_builtin_shell_inside_macos_seatbelt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def run() -> object:
        runtime, services, _ = _runtime(tmp_path)
        project_dir = tmp_path / "sandboxed_shell_project"
        project_dir.mkdir()
        monkeypatch.setenv("SMITH_RUNTIME_SECRET", "must-not-leak")
        (runtime.profile_dir / "config.yaml").write_text(
            "tools:\n  enabled: [shell]\n", encoding="utf-8"
        )
        shell_source = (
            Path(__file__).resolve().parents[3] / "agents" / "tools" / "shell.py"
        )
        (runtime.agents_dir / "tools" / "shell.py").write_text(
            shell_source.read_text(encoding="utf-8"), encoding="utf-8"
        )
        await prepare_runtime(
            EngineRequest(message="Inspect the project", working_dir=str(project_dir)),
            runtime,
            services,
        )
        return await services.tool_registry.execute(
            ToolCall(
                id="shell",
                name="shell",
                arguments={"command": 'printf "%s|%s" "$SMITH_RUNTIME_SECRET" "$HOME"'},
            )
        )

    result = asyncio.run(run())

    assert not result.is_error, result.content
    assert "must-not-leak" not in result.content
    assert "sandboxed_shell_project" in result.content


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_prepare_runtime_seals_registry_against_unapproved_direct_shell_execution(
    tmp_path: Path,
) -> None:
    async def run() -> tuple[object, Path]:
        runtime, services, _ = _runtime(tmp_path)
        project_dir = tmp_path / "sealed_shell_project"
        project_dir.mkdir()
        marker = project_dir / "must-not-exist.txt"
        (runtime.profile_dir / "config.yaml").write_text(
            "tools:\n  enabled: [shell]\n", encoding="utf-8"
        )
        shell_source = (
            Path(__file__).resolve().parents[3] / "agents" / "tools" / "shell.py"
        )
        (runtime.agents_dir / "tools" / "shell.py").write_text(
            shell_source.read_text(encoding="utf-8"), encoding="utf-8"
        )
        services.tool_guard = ToolGuard(tmp_path / "missing-rules.json")

        await prepare_runtime(
            EngineRequest(message="Inspect the project", working_dir=str(project_dir)),
            runtime,
            services,
        )
        result = await services.tool_registry.execute(
            ToolCall(
                id="unapproved-shell",
                name="shell",
                arguments={"command": "touch must-not-exist.txt"},
            )
        )
        return result, marker

    result, marker = asyncio.run(run())

    assert result.is_error
    assert result.error_kind == "approval_required"
    assert not marker.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_run_stream_executes_shell_only_after_approval_through_sandbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sandbox_calls: list[dict[str, object]] = []

    class RecordingSeatbelt(MacOSSeatbeltEnvironment):
        def __init__(self, *, workspace: str | Path) -> None:
            super().__init__(workspace=workspace)
            self.workspace = Path(workspace).resolve()

        async def run_command(
            self,
            command=None,
            *,
            argv=None,
            cwd=None,
            timeout_seconds=30.0,
            env=None,
        ) -> CommandResult:
            sandbox_calls.append(
                {
                    "command": command,
                    "argv": argv,
                    "cwd": cwd,
                    "timeout_seconds": timeout_seconds,
                    "env": env,
                    "workspace": self.workspace,
                }
            )
            return await super().run_command(
                command,
                argv=argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                env=env,
            )

    monkeypatch.setattr(
        preparation_module,
        "MacOSSeatbeltEnvironment",
        RecordingSeatbelt,
    )

    async def run() -> tuple[str, list[ExecutionEvent], Path, Path, Path]:
        runtime, services, _ = _runtime(tmp_path)
        project_dir = tmp_path / "approved_shell_project"
        audit_path = tmp_path / "audit.jsonl"
        project_dir.mkdir()
        (runtime.profile_dir / "config.yaml").write_text(
            "tools:\n  enabled: [shell]\n", encoding="utf-8"
        )
        shell_source = (
            Path(__file__).resolve().parents[3] / "agents" / "tools" / "shell.py"
        )
        (runtime.agents_dir / "tools" / "shell.py").write_text(
            shell_source.read_text(encoding="utf-8"), encoding="utf-8"
        )
        rules = Path(__file__).resolve().parents[3] / "agents" / "safety" / "dangerous_commands.json"
        services.tool_guard = ToolGuard(rules)
        services.tool_guard.audit = AuditLog(audit_path)
        services.llm = ShellCallingLLM()  # type: ignore[assignment]

        stream = run_stream_with_runtime(
            EngineRequest(message="Run pwd", working_dir=str(project_dir)),
            runtime,
            services,
        )
        events: list[ExecutionEvent] = []
        async for event in stream.stream_events():
            events.append(event)
            if (
                event.type is EventType.TOOL_CALL_RESULT
                and event.data.get("approval_required")
            ):
                assert APPROVAL_BROKER.resolve(
                    stream.run_id,
                    str(event.data["approval_id"]),
                    True,
                )
        return stream.run_id, events, project_dir, runtime.profile_dir, audit_path

    run_id, events, project_dir, profile_dir, audit_path = asyncio.run(run())

    assert len(sandbox_calls) == 1, [
        (event.type.value, event.data) for event in events
        if event.type in {
            EventType.TOOL_CALL_RESULT,
            EventType.FAILED,
            EventType.INCOMPLETE,
            EventType.RUN_FINISHED,
        }
    ]
    assert sandbox_calls[0]["command"] == "pwd"
    assert sandbox_calls[0]["cwd"] == str(project_dir.resolve())
    assert sandbox_calls[0]["workspace"] == project_dir.resolve()
    result_event = next(
        event
        for event in events
        if event.type is EventType.TOOL_CALL_RESULT
        and event.data.get("id") == "shell-call-1"
        and event.data.get("approval_outcome") == "granted"
    )
    assert result_event.data["blocked"] is False
    assert result_event.data["error"] is False
    assert result_event.data["side_effect_status"] == "completed"
    assert str(project_dir.resolve()) in str(result_event.data["content"])
    assert events[-1].type is EventType.RUN_FINISHED
    assert events[-1].data == {"run_id": run_id, "status": "completed"}
    assert (profile_dir / "runs" / "tool_executions.sqlite").is_file()
    audit_entry = next(
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["tool"] == "shell"
    )
    assert audit_entry["run_id"] == run_id
    assert audit_entry["call_id"] == "shell-call-1"
    traced_result = next(
        record
        for record in TraceStore(profile_dir).read(run_id)
        if record["type"] == EventType.TOOL_CALL_RESULT.value
        and record["data"].get("id") == audit_entry["call_id"]
        and record["data"].get("approval_outcome") == "granted"
    )
    assert traced_result["data"]["side_effect_status"] == "completed"


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_approved_shell_cannot_read_a_globbed_workspace_secret(
    tmp_path: Path,
) -> None:
    async def run() -> tuple[list[ExecutionEvent], Path]:
        runtime, services, _ = _runtime(tmp_path)
        project_dir = tmp_path / "protected_shell_project"
        project_dir.mkdir()
        (project_dir / ".env").write_text(
            "SMITH_SECRET=must-not-leak\n",
            encoding="utf-8",
        )
        (runtime.profile_dir / "config.yaml").write_text(
            "tools:\n  enabled: [shell]\n",
            encoding="utf-8",
        )
        shell_source = (
            Path(__file__).resolve().parents[3] / "agents" / "tools" / "shell.py"
        )
        (runtime.agents_dir / "tools" / "shell.py").write_text(
            shell_source.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        rules = (
            Path(__file__).resolve().parents[3]
            / "agents"
            / "safety"
            / "dangerous_commands.json"
        )
        services.tool_guard = ToolGuard(rules)
        services.llm = ShellCallingLLM(command="/bin/cat ./.en?")  # type: ignore[assignment]

        stream = run_stream_with_runtime(
            EngineRequest(
                message="Read the globbed file",
                working_dir=str(project_dir),
            ),
            runtime,
            services,
        )
        events: list[ExecutionEvent] = []
        async for event in stream.stream_events():
            events.append(event)
            if (
                event.type is EventType.TOOL_CALL_RESULT
                and event.data.get("approval_required")
            ):
                assert APPROVAL_BROKER.resolve(
                    stream.run_id,
                    str(event.data["approval_id"]),
                    True,
                )
        return events, project_dir

    events, project_dir = asyncio.run(run())

    result_event = next(
        event
        for event in events
        if event.type is EventType.TOOL_CALL_RESULT
        and event.data.get("id") == "shell-call-1"
        and event.data.get("approval_outcome") == "granted"
    )
    assert result_event.data["blocked"] is False
    assert "must-not-leak" not in str(result_event.data["content"])
    assert "[exit_code=1]" in str(result_event.data["content"])
    assert (project_dir / ".env").read_text(encoding="utf-8") == (
        "SMITH_SECRET=must-not-leak\n"
    )
    assert events[-1].type is EventType.RUN_FINISHED
    assert events[-1].data["status"] == "completed"


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_approved_shell_cannot_write_globbed_git_metadata(
    tmp_path: Path,
) -> None:
    async def run() -> tuple[list[ExecutionEvent], Path]:
        runtime, services, _ = _runtime(tmp_path)
        project_dir = tmp_path / "protected_git_project"
        git_config = project_dir / ".git" / "config"
        git_config.parent.mkdir(parents=True)
        git_config.write_text("original\n", encoding="utf-8")
        (runtime.profile_dir / "config.yaml").write_text(
            "tools:\n  enabled: [shell]\n",
            encoding="utf-8",
        )
        shell_source = (
            Path(__file__).resolve().parents[3] / "agents" / "tools" / "shell.py"
        )
        (runtime.agents_dir / "tools" / "shell.py").write_text(
            shell_source.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        rules = (
            Path(__file__).resolve().parents[3]
            / "agents"
            / "safety"
            / "dangerous_commands.json"
        )
        services.tool_guard = ToolGuard(rules)
        services.llm = RetryingShellCallingLLM(  # type: ignore[assignment]
            command="printf compromised > .gi?/config"
        )

        stream = run_stream_with_runtime(
            EngineRequest(
                message="Update the globbed metadata file",
                working_dir=str(project_dir),
            ),
            runtime,
            services,
        )
        events: list[ExecutionEvent] = []
        async for event in stream.stream_events():
            events.append(event)
            if (
                event.type is EventType.TOOL_CALL_RESULT
                and event.data.get("approval_required")
            ):
                assert APPROVAL_BROKER.resolve(
                    stream.run_id,
                    str(event.data["approval_id"]),
                    True,
                )
        return events, git_config

    events, git_config = asyncio.run(run())

    assert any(
        event.type is EventType.TOOL_CALL_RESULT
        and event.data.get("id") == "shell-call-preflight"
        and event.data.get("preflight")
        for event in events
    )
    result_event = next(
        event
        for event in events
        if event.type is EventType.TOOL_CALL_RESULT
        and event.data.get("id") == "shell-call-approved"
        and event.data.get("approval_outcome") == "granted"
    )
    assert result_event.data["blocked"] is False
    assert "[exit_code=1]" in str(result_event.data["content"])
    assert git_config.read_text(encoding="utf-8") == "original\n"
    assert events[-1].type is EventType.RUN_FINISHED
    assert events[-1].data["status"] == "completed"


def test_prepare_runtime_binds_memory_ops_but_keeps_it_hidden(tmp_path: Path) -> None:
    async def run() -> tuple[list[str], list[dict]]:
        runtime, services, _ = _runtime(tmp_path)
        tools_dir = runtime.agents_dir / "tools"
        tools_dir.mkdir(exist_ok=True)
        memory_ops_src = Path(__file__).resolve().parents[3] / "agents" / "tools" / "memory_ops.py"
        (tools_dir / "memory_ops.py").write_text(memory_ops_src.read_text(encoding="utf-8"), encoding="utf-8")
        (runtime.profile_dir / "config.yaml").write_text("tools:\n  enabled: [memory_ops]\n", encoding="utf-8")

        await prepare_runtime(EngineRequest(message="hello"), runtime, services)
        return (
            services.tool_registry.list_tool_names(include_disabled=True),
            services.tool_registry.get_schemas(),
        )

    tool_names, schemas = asyncio.run(run())

    assert "memory_ops" in tool_names
    assert all(schema["function"]["name"] != "memory_ops" for schema in schemas)


def test_prepare_runtime_excludes_a_disabled_skill_from_the_live_registry(tmp_path: Path) -> None:
    async def run() -> bool:
        runtime, services, _ = _runtime(tmp_path)
        skill_dir = runtime.profile_dir / "skills" / "research"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: research\ndescription: Research a topic.\n---\nResearch.",
            encoding="utf-8",
        )
        (runtime.profile_dir / "skills.yaml").write_text("disabled: [research]\n", encoding="utf-8")

        setup = await prepare_runtime(EngineRequest(message="Research this"), runtime, services)
        return (
            services.skill_registry.get("research") is None
            and setup.disabled_skill_names == frozenset({"research"})
        )

    assert asyncio.run(run())


def test_prepare_runtime_keeps_recent_and_retrieves_only_matching_durable(
    tmp_path: Path,
) -> None:
    async def run() -> tuple[str, dict[str, object]]:
        runtime, services, _ = _runtime(tmp_path)
        memory_dir = runtime.profile_dir / "memory"
        memory_dir.mkdir()
        (memory_dir / "recent.md").write_text(
            "# Recent Working Memory\n\n## Active Work\n- RECENT_ACTIVE_WORK\n",
            encoding="utf-8",
        )
        (memory_dir / "durable.md").write_text(
            "# Durable Project Memory\n\n"
            "## Confirmed Facts\n"
            "- PostgreSQL migration uses Alembic.\n"
            "- Redis caching uses a separate worker.\n",
            encoding="utf-8",
        )

        setup = await prepare_runtime(
            EngineRequest(message="Continue the PostgreSQL migration"),
            runtime,
            services,
        )
        return setup.system_prompt, setup.prompt_manifest

    prompt, manifest = asyncio.run(run())

    assert "RECENT_ACTIVE_WORK" in prompt
    assert "PostgreSQL migration uses Alembic" in prompt
    assert "Redis caching uses a separate worker" not in prompt
    assert "## Context: Recent Working Context" in prompt
    assert "## Context: Durable Memory Retrieval" in prompt
    layers = {item["id"]: item for item in manifest["layers"]}  # type: ignore[index]
    assert layers["recent_working_context"]["source"] == "memory_recent"
    assert layers["recent_working_context"]["action"] == "loaded"
    assert layers["durable_retrieval"]["source"] == "memory_durable"
    assert layers["durable_retrieval"]["action"] == "loaded"


def test_prepare_runtime_injects_engine_owned_runtime_control(tmp_path: Path) -> None:
    async def run() -> str:
        runtime, services, _ = _runtime(tmp_path)
        setup = await prepare_runtime(EngineRequest(message="Inspect this project"), runtime, services)
        return setup.system_prompt

    prompt = asyncio.run(run())

    assert "## Engine Runtime Control" in prompt
    assert "ToolPolicy" in prompt
    assert prompt.endswith("only when the task remains unfinished.")


def test_prepare_runtime_applies_system_prompt_hook_and_updates_receipt(
    tmp_path: Path,
) -> None:
    class PromptHook:
        async def system_prompt(self, prompt: str) -> str:
            return prompt + "\n\nHOOK_MARKER"

    async def run():
        runtime, services, _ = _runtime(tmp_path)
        services.hooks = HookManager()
        services.hooks.register(PromptHook())
        return await prepare_runtime(
            EngineRequest(message="Inspect this project"),
            runtime,
            services,
        )

    setup = asyncio.run(run())
    layers = {
        item["id"]: item
        for item in setup.prompt_manifest["layers"]  # type: ignore[index]
    }

    assert setup.system_prompt.endswith("HOOK_MARKER")
    assert layers["system_prompt_hooks"]["action"] == "modified"
    assert setup.prompt_manifest["rendered_prompt_hash"] == hashlib.sha256(
        setup.system_prompt.encode()
    ).hexdigest()
    assert len(setup.prefix_cache_key) == 64


def test_run_stream_persists_redacted_prompt_manifest_to_private_trace(tmp_path: Path) -> None:
    async def run() -> tuple[Path, str]:
        runtime, services, _ = _runtime(tmp_path)
        (runtime.profile_dir / "role.md").write_text("ROLE_SECRET_VALUE", encoding="utf-8")
        stream = run_stream_with_runtime(EngineRequest(message="hello"), runtime, services)
        _ = [event async for event in stream.stream_events()]
        return runtime.profile_dir, stream.run_id

    profile_dir, run_id = asyncio.run(run())
    records = TraceStore(profile_dir).read(run_id)
    manifest = next(record for record in records if record["type"] == "prompt_manifest")

    assert manifest["data"]["schema_version"] == 1
    assert manifest["data"]["rendered_prompt_hash"]
    assert any(layer["id"] == "role" for layer in manifest["data"]["layers"])
    assert "ROLE_SECRET_VALUE" not in str(manifest)


def test_run_stream_persists_a_terminal_summary(tmp_path: Path) -> None:
    async def run() -> tuple[Path, str, str]:
        runtime, services, _ = _runtime(tmp_path)
        stream = run_stream_with_runtime(EngineRequest(message="hello"), runtime, services)
        _ = [event async for event in stream.stream_events()]
        return runtime.profile_dir, stream.run_id, runtime.agent_id

    profile_dir, run_id, agent_id = asyncio.run(run())
    record = RunSummaryStore(profile_dir).get(run_id)

    assert record is not None
    assert record.metadata.agent_id == agent_id
    assert record.summary.outcome == "completed"
    assert record.summary.event_count > 0


def test_run_stream_is_not_failed_by_an_observer_adapter(tmp_path: Path) -> None:
    class FailingObserver:
        def record(self, event: ExecutionEvent) -> None:
            raise OSError("observer unavailable")

        def append_prompt_manifest(self, manifest: dict[str, object]) -> None:
            raise OSError("observer unavailable")

    async def run() -> list[ExecutionEvent]:
        runtime, services, _ = _runtime(tmp_path)
        services.observation_factory = lambda _context: FailingObserver()
        stream = run_stream_with_runtime(EngineRequest(message="hello"), runtime, services)
        return [event async for event in stream.stream_events()]

    events = asyncio.run(run())

    assert events[-1].type is EventType.RUN_FINISHED
    assert events[-1].data["status"] == "completed"


def test_reply_with_runtime_uses_explicit_profile_context(tmp_path: Path) -> None:
    async def run() -> FakeLLM:
        runtime, services, llm = _runtime(tmp_path)
        result = await reply_with_runtime(
            EngineRequest(message="hello", context="cwd=/tmp/work"),
            runtime,
            services,
        )

        assert result.text == "runtime reply"
        assert result.had_tools is False
        assert not (runtime.profile_dir / "identity-state" / "smith" / "memory" / "recent.jsonl").exists()
        return llm

    llm = asyncio.run(run())
    assert llm.closed is True
    assert llm.messages[-1]["content"] == "hello\n\ncwd=/tmp/work"
    assert "agent_id: smith" in llm.messages[0]["content"]
    assert "_profile_dir:" in llm.messages[0]["content"]


def test_runtime_carries_assembled_prefix_key_to_capable_llm(tmp_path: Path) -> None:
    class PrefixCapableLLM(FakeLLM):
        capabilities = ProviderCapabilities(prefix_cache_key=True)

        def __init__(self) -> None:
            super().__init__()
            self.prefix_cache_key: str | None = None

        async def chat(
            self,
            messages: list[dict],
            tools: list[dict] | None = None,
            prefix_cache_key: str | None = None,
        ) -> ChatResponse:
            self.prefix_cache_key = prefix_cache_key
            return await super().chat(messages, tools, prefix_cache_key)

    async def run():
        runtime, services, _ = _runtime(tmp_path)
        llm = PrefixCapableLLM()
        services.llm = llm  # type: ignore[assignment]
        stream = run_stream_with_runtime(
            EngineRequest(message="answer directly"),
            runtime,
            services,
        )
        events = [event async for event in stream.stream_events()]
        return runtime, llm, events

    runtime, llm, events = asyncio.run(run())

    assert any(event.type is EventType.TEXT_DELTA for event in events)
    assert llm.prefix_cache_key is not None
    assert len(llm.prefix_cache_key) == 64
    assert (
        preparation_module.PromptAssembler.get_prefix_cache_key(runtime.profile_dir)
        == llm.prefix_cache_key
    )


def test_run_stream_persists_queued_and_terminal_run_state(tmp_path: Path) -> None:
    async def run() -> tuple[str, RunStatus, int]:
        runtime, services, _ = _runtime(tmp_path)
        stream = run_stream_with_runtime(EngineRequest(message="hello"), runtime, services)
        store = RunStateStore(runtime.profile_dir)
        queued = store.get(stream.run_id)
        assert queued is not None
        assert queued.status is RunStatus.QUEUED

        events = [event async for event in stream.stream_events()]
        finished = store.get(stream.run_id)
        assert finished is not None
        assert finished.status is RunStatus.COMPLETED
        assert finished.last_event_type == EventType.RUN_FINISHED.value
        assert finished.event_seq == len(events)
        return stream.run_id, finished.status, finished.event_seq

    run_id, status, event_seq = asyncio.run(run())
    assert run_id
    assert status is RunStatus.COMPLETED
    assert event_seq > 1


def test_timeout_settles_waiting_approval_before_terminal_run_state(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path)
    store.create("run-1", agent_id="smith")
    project_execution_event(
        store,
        "run-1",
        ExecutionEvent(EventType.RUN_STARTED, {"run_id": "run-1"}),
    )
    project_execution_event(
        store,
        "run-1",
        ExecutionEvent(
            EventType.TOOL_CALL_RESULT,
            {
                "approval_required": True,
                "approval_id": "approval-1",
                "tool": "shell",
                "level": "execute",
                "reason": "Approval required for shell",
            },
        ),
    )
    project_execution_event(
        store,
        "run-1",
        ExecutionEvent(
            EventType.TOOL_CALL_RESULT,
            {
                "approval_id": "approval-1",
                "approval_outcome": "timed_out",
                "blocked": True,
                "reason": "Approval timed out",
            },
        ),
    )

    settled = store.get("run-1")
    assert settled is not None
    assert settled.status is RunStatus.RUNNING
    assert settled.reason == "approval_timed_out"
    assert settled.approval_id is None

    project_execution_event(
        store,
        "run-1",
        ExecutionEvent(
            EventType.RUN_FINISHED,
            {"run_id": "run-1", "status": "completed"},
        ),
    )
    terminal = store.get("run-1")
    assert terminal is not None and terminal.status is RunStatus.COMPLETED


def test_granted_approval_resolves_waiting_approval_to_running(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path)
    store.create("run-1", agent_id="smith")
    project_execution_event(
        store,
        "run-1",
        ExecutionEvent(EventType.RUN_STARTED, {"run_id": "run-1"}),
    )
    project_execution_event(
        store,
        "run-1",
        ExecutionEvent(
            EventType.TOOL_CALL_RESULT,
            {
                "approval_required": True,
                "approval_id": "approval-1",
                "tool": "shell",
                "level": "execute",
                "reason": "Approval required for shell",
            },
        ),
    )
    waiting = store.get("run-1")
    assert waiting is not None and waiting.status is RunStatus.WAITING_APPROVAL

    project_execution_event(
        store,
        "run-1",
        ExecutionEvent(
            EventType.TOOL_CALL_RESULT,
            {
                "approval_id": "approval-1",
                "approval_outcome": "granted",
                "blocked": False,
            },
        ),
    )

    resolved = store.get("run-1")
    assert resolved is not None
    assert resolved.status is RunStatus.RUNNING
    assert resolved.approval_id is None
    assert resolved.reason == "approval_granted"

    project_execution_event(
        store,
        "run-1",
        ExecutionEvent(
            EventType.RUN_FINISHED,
            {"run_id": "run-1", "status": "completed"},
        ),
    )
    terminal = store.get("run-1")
    assert terminal is not None and terminal.status is RunStatus.COMPLETED


def test_resume_setup_failure_is_exposed_as_terminal_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, services, llm = _runtime(tmp_path)

    def fail_store(_profile_dir: Path):
        raise OSError("runs directory unavailable")

    monkeypatch.setattr(lifecycle_module, "RunStateStore", fail_store)

    async def collect():
        stream = resume_stream_with_runtime(
            EngineRequest(message="resume"), runtime, services, "missing-run"
        )
        return stream, [event async for event in stream.stream_events()]

    stream, events = asyncio.run(collect())

    assert events[-1].type is EventType.RUN_FINISHED
    assert events[-1].data == {
        "run_id": "missing-run",
        "status": "failed",
        "reason": "resume_setup_failed",
    }
    assert stream.is_complete is True
    assert stream.status == "failed"
    assert llm.closed is True


def test_resume_stream_cleans_up_when_closed_before_first_event(tmp_path: Path) -> None:
    async def run():
        runtime, services, llm = _runtime(tmp_path)
        store = RunStateStore(runtime.profile_dir)
        store.create("run-1", agent_id=runtime.agent_id)
        store.transition("run-1", RunStatus.RUNNING)
        store.transition("run-1", RunStatus.INCOMPLETE)

        stream = resume_stream_with_runtime(
            EngineRequest(message="resume"), runtime, services, "run-1"
        )
        await stream.aclose()
        return RunStateStore(runtime.profile_dir).get("run-1"), llm

    state, llm = asyncio.run(run())

    assert state is not None
    assert state.status is RunStatus.CANCELLED
    assert state.reason == "consumer_disconnected"
    assert llm.closed is True


def test_resume_stream_enables_ledger_replay_for_new_provider_call_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, services, _ = _runtime(tmp_path)
    store = RunStateStore(runtime.profile_dir)
    store.create("run-1", agent_id=runtime.agent_id)
    store.transition("run-1", RunStatus.RUNNING)
    store.transition("run-1", RunStatus.INCOMPLETE)
    captured: dict[str, object] = {}

    class RecordingLedger:
        def __init__(self, profile_dir: Path, run_id: str, *, replay_existing: bool = False):
            captured["profile_dir"] = profile_dir
            captured["run_id"] = run_id
            captured["replay_existing"] = replay_existing

    async def fake_events(*_args, **_kwargs):
        if False:
            yield None

    monkeypatch.setattr(lifecycle_module, "ToolExecutionLedger", RecordingLedger)
    monkeypatch.setattr(lifecycle_module, "_run_events_with_runtime", fake_events)

    resume_stream_with_runtime(EngineRequest(message="resume"), runtime, services, "run-1")

    assert captured == {
        "profile_dir": runtime.profile_dir,
        "run_id": "run-1",
        "replay_existing": True,
    }


def test_incomplete_run_persists_learning_with_partial_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, services, _ = _runtime(tmp_path)
    identity = runtime.identity_catalog.get("smith")  # type: ignore[union-attr]
    setup = SimpleNamespace(
        system_prompt="system",
        identity=identity,
        route=SimpleNamespace(identity_id="smith", route_id="direct", pipeline_id=None),
        chain=None,
        state_dir=runtime.profile_dir,
        working_dir=tmp_path,
    )
    captured: dict[str, object] = {}

    async def fake_prepare_runtime(*_args, **_kwargs):
        return setup

    async def fake_run_agent_stream(*_args, **_kwargs):
        yield ExecutionEvent(EventType.TOOL_CALL_START, {"name": "search"})
        yield ExecutionEvent(EventType.TEXT_DELTA, {"text": "partial result"})
        yield ExecutionEvent(EventType.INCOMPLETE, {"reason": "model_output_limit"})

    async def fake_persist(*_args, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(lifecycle_module, "prepare_runtime", fake_prepare_runtime)
    monkeypatch.setattr(lifecycle_module, "run_agent_stream", fake_run_agent_stream)
    monkeypatch.setattr(lifecycle_module, "_persist_runtime_learning", fake_persist)

    async def collect():
        stream = run_stream_with_runtime(EngineRequest(message="continue"), runtime, services)
        return [event async for event in stream.stream_events()]

    events = asyncio.run(collect())

    assert captured == {
        "terminal_status": "incomplete",
        "terminal_reason": "model_output_limit",
    }
    assert events[-1].data["status"] == "incomplete"


def test_runtime_memory_requires_a_successful_tool_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proposed, blocked, preflight, or failed tool call is not evidence."""

    async def run_case(case_name: str, result_data: dict[str, object]) -> bool:
        runtime, services, _ = _runtime(tmp_path / case_name)
        identity = runtime.identity_catalog.get("smith")  # type: ignore[union-attr]
        setup = SimpleNamespace(
            system_prompt="system",
            identity=identity,
            route=SimpleNamespace(identity_id="smith", route_id="direct", pipeline_id=None),
            chain=None,
            state_dir=runtime.profile_dir,
            working_dir=tmp_path / case_name,
        )
        captured: dict[str, object] = {}

        async def fake_prepare_runtime(*_args, **_kwargs):
            return setup

        async def fake_run_agent_stream(*_args, **_kwargs):
            yield ExecutionEvent(EventType.SKILL_START, {"skill": "planning"})
            yield ExecutionEvent(EventType.TOOL_CALL_START, {"name": "search"})
            yield ExecutionEvent(EventType.TOOL_CALL_RESULT, result_data)
            yield ExecutionEvent(EventType.TEXT_DELTA, {"text": "final reply"})
            yield ExecutionEvent(EventType.DONE, {})

        async def fake_persist(*args, **_kwargs):
            captured["had_tools"] = args[3]
            return True

        monkeypatch.setattr(lifecycle_module, "prepare_runtime", fake_prepare_runtime)
        monkeypatch.setattr(lifecycle_module, "run_agent_stream", fake_run_agent_stream)
        monkeypatch.setattr(lifecycle_module, "_persist_runtime_learning", fake_persist)

        stream = run_stream_with_runtime(EngineRequest(message="continue"), runtime, services)
        _ = [event async for event in stream.stream_events()]
        return bool(captured["had_tools"])

    blocked = asyncio.run(run_case("blocked", {"blocked": True, "preflight": False, "error": False}))
    preflight = asyncio.run(run_case("preflight", {"blocked": False, "preflight": True, "error": False}))
    failed = asyncio.run(run_case("failed", {"blocked": False, "preflight": False, "error": True}))
    successful = asyncio.run(run_case("successful", {"blocked": False, "preflight": False, "error": False}))

    assert blocked is False
    assert preflight is False
    assert failed is False
    assert successful is True


def test_raw_text_delta_uses_normalized_provider_event_contract() -> None:
    event = ExecutionEvent(
        EventType.RAW_RESPONSE_EVENT,
        {
            "type": "response.output_text.delta",
            "data": {"delta": "hello"},
        },
    )
    provisional = ExecutionEvent(
        EventType.RAW_RESPONSE_EVENT,
        {
            "type": "response.output_text.delta",
            "provision_id": "draft-1",
            "data": {"delta": "draft"},
        },
    )

    assert raw_text_delta(event, include_provisional=False) == "hello"
    assert raw_text_delta(provisional, include_provisional=False) is None
    assert raw_text_delta(provisional) == "draft"


def test_lifecycle_public_exports_exclude_react_implementation_aliases() -> None:
    assert "run_stream_with_runtime" in lifecycle_module.__all__
    assert "_react_event_loop" not in lifecycle_module.__all__
    assert "_react_loop" not in lifecycle_module.__all__
    assert "_react_stream_loop" not in lifecycle_module.__all__


def test_run_stream_bounds_post_run_learning_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_persist(*_args, **_kwargs):
        await asyncio.sleep(1)
        return True

    monkeypatch.setattr(lifecycle_module, "_persist_runtime_learning", slow_persist)
    monkeypatch.setattr(lifecycle_module, "_RUNTIME_LEARNING_TIMEOUT_SECONDS", 0.01, raising=False)

    async def collect_events():
        runtime, services, _ = _runtime(tmp_path)
        stream = run_stream_with_runtime(EngineRequest(message="hello"), runtime, services)
        return [event async for event in stream.stream_events()]

    events = asyncio.run(asyncio.wait_for(collect_events(), timeout=0.2))

    assert events[-1].type is EventType.RUN_FINISHED


def test_reply_with_runtime_marks_actual_tool_activity(tmp_path: Path) -> None:
    async def run() -> ToolCallingLLM:
        runtime, services, _ = _runtime(tmp_path)
        _register_successful_test_tool(runtime, services)
        llm = ToolCallingLLM()
        services.llm = llm  # type: ignore[assignment]

        result = await reply_with_runtime(EngineRequest(message="use a tool"), runtime, services)

        assert result.text == "tool-assisted reply"
        assert result.had_tools is True
        assert (runtime.profile_dir / "memory" / "recent.jsonl").is_file()
        return llm

    llm = asyncio.run(run())
    assert llm.closed is True


def test_runtime_reuses_llm_for_recent_memory_compilation(tmp_path: Path) -> None:
    async def run() -> ToolCallingMemoryLLM:
        runtime, services, _ = _runtime(tmp_path)
        _register_successful_test_tool(runtime, services)
        llm = ToolCallingMemoryLLM()
        services.llm = llm  # type: ignore[assignment]
        services.gate_llm = PassReviewer()  # type: ignore[assignment]
        state_dir = runtime.profile_dir / "memory"
        state_dir.mkdir(parents=True)
        (state_dir / ".compile_counter").write_text("4", encoding="utf-8")

        result = await reply_with_runtime(EngineRequest(message="use a tool"), runtime, services)

        assert result.had_tools is True
        assert (state_dir / "recent.md").is_file()
        assert not (state_dir / "durable.md").exists()
        assert (state_dir / ".compile_counter").read_text(encoding="utf-8") == "0"
        return llm

    llm = asyncio.run(run())

    assert llm.closed is True
    assert len(llm.chat_calls) >= 2
    assert any(
        messages[0]["content"].startswith("You are Smith's memory compiler")
        for messages in llm.chat_calls
    )


def test_prepare_runtime_resolves_a_yaml_route_to_its_pipeline(tmp_path: Path) -> None:
    async def run():
        runtime, services, _ = _runtime(tmp_path)
        identities_dir = runtime.agents_dir / "identities"
        (identities_dir / "smith.yaml").write_text(
            """
schema: agentsmith.identity/v1
id: smith
name: Smith
default: true
routes:
  - id: refactor
    keywords: [重构]
    pipeline: refactor
""".strip(),
            encoding="utf-8",
        )
        pipelines_dir = runtime.agents_dir / "pipelines"
        pipelines_dir.mkdir()
        (pipelines_dir / "refactor.yaml").write_text(
            """
name: refactor
route: refactor
steps:
  - skill: planning
    gate: runtime_contract_planning
""".strip(),
            encoding="utf-8",
        )
        gates_dir = runtime.agents_dir / "gates"
        gates_dir.mkdir()
        (gates_dir / "planning.py").write_text(
            """
from engine.execution.pipeline.gate import Gate, GateResult

class AlwaysPassGate(Gate):
    async def check(self, output, context):
        return GateResult("pass", "ok")

GATES = {"runtime_contract_planning": AlwaysPassGate}
""".strip(),
            encoding="utf-8",
        )
        runtime = RuntimeContext(
            agent_id=runtime.agent_id,
            agent_name=runtime.agent_name,
            profile_dir=runtime.profile_dir,
            agents_dir=runtime.agents_dir,
            session_id=runtime.session_id,
            identity_catalog=IdentityCatalog.load(identities_dir),
        )
        return await prepare_runtime(EngineRequest(message="请重构这个模块"), runtime, services)

    setup = asyncio.run(run())

    assert setup.route.identity_id == "smith"
    assert setup.route.route_id == "refactor"
    assert setup.route.pipeline_id == "refactor"
    assert setup.chain is not None
    assert [node.skill_name for node in setup.chain.nodes] == ["planning"]


def test_shipped_coding_pipeline_routes_through_gates_and_conditions(tmp_path: Path) -> None:
    async def run() -> tuple[object, list[ExecutionEvent]]:
        runtime, services, _ = _runtime(tmp_path)
        agents_dir = Path(__file__).resolve().parents[3] / "agents"
        skills_dir = runtime.profile_dir / "skills"
        for skill_name in ("understanding", "planning", "architecture", "implementation", "validation"):
            skill_dir = skills_dir / skill_name
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: {skill_name}\ndescription: test workflow step\n---\nPerform this workflow step.",
                encoding="utf-8",
            )
        runtime = RuntimeContext(
            agent_id=runtime.agent_id,
            agent_name=runtime.agent_name,
            profile_dir=runtime.profile_dir,
            agents_dir=agents_dir,
            session_id=runtime.session_id,
            identity_catalog=IdentityCatalog.load(agents_dir / "identities"),
        )
        llm = CodingPipelineLLM()
        services.llm = llm  # type: ignore[assignment]
        services.gate_llm = LlmGatePassReviewer()  # type: ignore[assignment]
        request = EngineRequest(message="修复登录报错")
        setup = await prepare_runtime(request, runtime, services)
        assert setup.chain is not None
        events = [
            event
            async for event in run_agent_stream(
                llm,
                setup.system_prompt,
                request.message,
                services.tool_registry,
                services.skill_registry,
                setup.route,
                setup.chain,
                FailureLoopGuard(),
                gate_llm=services.gate_llm,
            )
        ]
        return setup, events

    setup, events = asyncio.run(run())

    assert setup.route.pipeline_id == "coding"
    assert [event.data["skill"] for event in events if event.type is EventType.SKILL_START] == [
        "understanding",
        "planning",
        "architecture",
        "implementation",
        "validation",
    ]
    assert [event.data["verdict"] for event in events if event.type is EventType.GATE_RESULT] == [
        "pass",
        "pass",
        "pass",
        "pass",
        "pass",
    ]
    assert events[-1].type is EventType.DONE


def test_reply_events_with_runtime_emits_decision_reply_and_closes(tmp_path: Path) -> None:
    async def run() -> tuple[list[str], FakeLLM]:
        runtime, services, llm = _runtime(tmp_path)
        chunks: list[str] = []
        async for event in reply_events_with_runtime(
            EngineRequest(message="hello"),
            runtime,
            services,
        ):
            if event.type == EventType.TEXT_DELTA:
                chunks.append(event.data["text"])
        return chunks, llm

    chunks, llm = asyncio.run(run())
    assert chunks == ["runtime reply"]
    assert llm.closed is True


def test_runtime_services_close_closes_the_gate_client(tmp_path: Path) -> None:
    runtime, services, llm = _runtime(tmp_path)
    gate_llm = FakeLLM()
    background_llm = FakeLLM()
    services.gate_llm = gate_llm  # type: ignore[assignment]
    services.background_llm = background_llm  # type: ignore[assignment]

    asyncio.run(services.close())

    assert runtime.agent_id == "smith"
    assert llm.closed is True
    assert gate_llm.closed is True
    assert background_llm.closed is True


def test_runtime_services_close_leaves_borrowed_llm_clients_open(tmp_path: Path) -> None:
    runtime, services, llm = _runtime(tmp_path)
    gate_llm = FakeLLM()
    background_llm = FakeLLM()
    services.gate_llm = gate_llm  # type: ignore[assignment]
    services.owns_llm_clients = False
    services.background_llm = background_llm  # type: ignore[assignment]

    asyncio.run(services.close())

    assert runtime.agent_id == "smith"
    assert llm.closed is False
    assert gate_llm.closed is False
    assert background_llm.closed is False


def test_run_stream_reports_terminal_state_only_after_it_is_drained(tmp_path: Path) -> None:
    async def run():
        runtime, services, _ = _runtime(tmp_path)
        stream = run_stream_with_runtime(EngineRequest(message="hello"), runtime, services)
        assert stream.is_complete is False
        events = [event async for event in stream.stream_events()]
        return stream, events

    stream, events = asyncio.run(run())

    assert events[0].type == EventType.RUN_STARTED
    assert events[-1].type == EventType.RUN_FINISHED
    assert events[-1].data["run_id"] == stream.run_id
    assert events[-1].data["status"] == "completed"
    assert stream.is_complete is True
    assert stream.status == "completed"


def test_run_stream_cleans_up_when_closed_immediately_after_start(tmp_path: Path) -> None:
    async def run():
        runtime, services, llm = _runtime(tmp_path)
        stream = run_stream_with_runtime(EngineRequest(message="hello"), runtime, services)
        events = stream.stream_events()
        first_event = await anext(events)
        await events.aclose()
        state = RunStateStore(runtime.profile_dir).get(stream.run_id)
        summary = RunSummaryStore(runtime.profile_dir).get(stream.run_id)
        return stream, first_event, state, summary, llm

    stream, first_event, state, summary, llm = asyncio.run(run())

    assert first_event.type is EventType.RUN_STARTED
    assert state is not None
    assert state.status is RunStatus.CANCELLED
    assert state.reason == "consumer_disconnected"
    assert summary is not None
    assert summary.summary.outcome == "cancelled"
    assert summary.summary.reason == "consumer_disconnected"
    assert llm.closed is True
    assert stream.is_complete is False


def test_run_stream_cleans_up_when_closed_before_first_event(tmp_path: Path) -> None:
    async def run():
        runtime, services, llm = _runtime(tmp_path)
        stream = run_stream_with_runtime(EngineRequest(message="hello"), runtime, services)
        events = stream.stream_events()
        await events.aclose()
        state = RunStateStore(runtime.profile_dir).get(stream.run_id)
        return stream, state, llm

    stream, state, llm = asyncio.run(run())

    assert state is not None
    assert state.status is RunStatus.CANCELLED
    assert state.reason == "consumer_disconnected"
    assert llm.closed is True
    assert stream.is_complete is False


def test_run_stream_owner_close_cleans_up_an_active_event_iterator(tmp_path: Path) -> None:
    async def run():
        runtime, services, llm = _runtime(tmp_path)
        stream = run_stream_with_runtime(EngineRequest(message="hello"), runtime, services)
        events = stream.stream_events()
        first_event = await anext(events)
        await stream.aclose()
        state = RunStateStore(runtime.profile_dir).get(stream.run_id)
        return first_event, state, llm

    first_event, state, llm = asyncio.run(run())

    assert first_event.type is EventType.RUN_STARTED
    assert state is not None
    assert state.status is RunStatus.CANCELLED
    assert state.reason == "consumer_disconnected"
    assert llm.closed is True


def test_run_stream_fails_closed_when_tool_ledger_setup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableLedger:
        def __init__(self, *_args, **_kwargs) -> None:
            raise OSError("ledger unavailable")

    async def run():
        runtime, services, llm = _runtime(tmp_path)
        stream = run_stream_with_runtime(EngineRequest(message="hello"), runtime, services)
        events = [event async for event in stream.stream_events()]
        state = RunStateStore(runtime.profile_dir).get(stream.run_id)
        return stream, events, state, llm

    monkeypatch.setattr(lifecycle_module, "ToolExecutionLedger", UnavailableLedger)
    stream, events, state, llm = asyncio.run(run())

    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.FAILED,
        EventType.DONE,
        EventType.RUN_FINISHED,
    ]
    assert events[-1].data == {
        "run_id": stream.run_id,
        "status": "failed",
        "reason": "tool_ledger_unavailable",
    }
    assert state is not None
    assert state.status is RunStatus.FAILED
    assert state.reason == "tool_ledger_unavailable"
    assert llm.closed is True


def test_reply_events_cleans_up_when_closed_after_start(tmp_path: Path) -> None:
    async def run():
        runtime, services, llm = _runtime(tmp_path)
        events = reply_events_with_runtime(EngineRequest(message="hello"), runtime, services)
        first_event = await anext(events)
        await events.aclose()
        state = RunStateStore(runtime.profile_dir).get(first_event.data["run_id"])
        return first_event, state, llm

    first_event, state, llm = asyncio.run(run())

    assert first_event.type is EventType.RUN_STARTED
    assert state is not None
    assert state.status is RunStatus.CANCELLED
    assert state.reason == "consumer_disconnected"
    assert llm.closed is True


def test_reply_events_with_runtime_reports_prepare_failure_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken_prepare(*args, **kwargs):
        raise RuntimeError("profile setup failed")

    async def run() -> tuple[list[EventType], FakeLLM]:
        runtime, services, llm = _runtime(tmp_path)
        events = []
        async for event in reply_events_with_runtime(
            EngineRequest(message="hello"),
            runtime,
            services,
        ):
            events.append(event)
        return [event.type for event in events], llm

    monkeypatch.setattr(lifecycle_module, "prepare_runtime", broken_prepare)
    event_types, llm = asyncio.run(run())

    assert event_types == [
        EventType.RUN_STARTED,
        EventType.TEXT_DELTA,
        EventType.FAILED,
        EventType.DONE,
        EventType.RUN_FINISHED,
    ]
    assert llm.closed is True


def test_run_stream_persists_redacted_provider_failure_details(tmp_path: Path) -> None:
    class ProviderFailureLLM(FakeLLM):
        provider = "openai"
        model = "test-model"

        async def chat(
            self,
            messages: list[dict],
            tools: list[dict] | None = None,
            prefix_cache_key: str | None = None,
        ) -> ChatResponse:
            raise LLMResponseError(
                "LLM request failed (HTTP 429); api_key=must-not-be-persisted"
            )

    async def run():
        runtime, services, _ = _runtime(tmp_path)
        services.llm = ProviderFailureLLM()  # type: ignore[assignment]
        stream = run_stream_with_runtime(
            EngineRequest(message="trigger provider request"),
            runtime,
            services,
        )
        events = [event async for event in stream.stream_events()]
        state = RunStateStore(runtime.profile_dir).get(stream.run_id)
        trace = TraceStore(runtime.profile_dir).read(stream.run_id)
        return events, state, trace

    events, state, trace = asyncio.run(run())
    failed = next(event for event in events if event.type is EventType.FAILED)
    finished = next(event for event in events if event.type is EventType.RUN_FINISHED)
    expected = {
        "kind": "provider_http",
        "stage": "agent_execution",
        "type": "LLMResponseError",
        "provider": "openai",
        "http_status": 429,
        "retryable": True,
    }

    assert failed.data == {"reason": "execution_error", "error": expected}
    assert finished.data["error"] == expected
    assert state is not None
    assert state.error_details == expected
    assert "must-not-be-persisted" not in str(trace)
