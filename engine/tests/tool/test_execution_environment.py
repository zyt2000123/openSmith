"""Tests for the execution environment boundary.

Covers the LocalExecutionEnvironment process lifecycle (spawn, timeout,
cancellation, output capping, environment failures) and the registry's
environment binding: injection into tool providers and rejection of tools
whose declared environment is unavailable. The process-group tests were
migrated from test_shell_safety.py when process handling moved out of the
shell provider.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from engine.safety.tool_guard import ToolGuard
from engine.sandbox import (
    MAX_OUTPUT,
    CommandResult,
    ExecutionEnvironment,
    LocalExecutionEnvironment,
)
from engine.sandbox import host as environment_module
from engine.tool.interface import ToolCall
from engine.tool.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[3]


class _PipeReader:
    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self._chunks = chunks or []

    async def read(self, _: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


def test_local_environment_satisfies_the_protocol() -> None:
    assert isinstance(LocalExecutionEnvironment(), ExecutionEnvironment)


def test_local_environment_runs_shell_commands() -> None:
    result = asyncio.run(LocalExecutionEnvironment().run_command("printf hello"))

    assert result.exit_code == 0
    assert result.stdout == "hello"
    assert not result.timed_out
    assert result.error is None


def test_local_environment_runs_argv_without_shell_interpretation() -> None:
    result = asyncio.run(
        LocalExecutionEnvironment().run_command(argv=["printf", "%s", "$HOME"])
    )

    assert result.exit_code == 0
    assert result.stdout == "$HOME"


def test_local_environment_requires_exactly_one_command_form() -> None:
    env = LocalExecutionEnvironment()

    both = asyncio.run(env.run_command("echo hi", argv=["echo", "hi"]))
    neither = asyncio.run(env.run_command())

    assert both.error and neither.error


def test_local_environment_reports_missing_binaries() -> None:
    result = asyncio.run(
        LocalExecutionEnvironment().run_command(argv=["definitely-not-a-binary-4242"])
    )

    assert result.exit_code is None
    assert result.error == "definitely-not-a-binary-4242 is not installed or not in PATH"


def test_local_environment_times_out_and_reports_it() -> None:
    started = time.monotonic()
    result = asyncio.run(
        LocalExecutionEnvironment().run_command("sleep 30", timeout_seconds=0.2)
    )

    assert result.timed_out
    assert result.exit_code is None
    assert time.monotonic() - started < 10


def test_local_environment_caps_stream_output() -> None:
    overflow = MAX_OUTPUT + 64
    result = asyncio.run(
        LocalExecutionEnvironment().run_command(
            argv=[sys.executable, "-c", f"print('x' * {overflow}, end='')"]
        )
    )

    assert result.exit_code == 0
    assert result.stdout.startswith("x" * MAX_OUTPUT)
    assert f"(truncated, {overflow} bytes total)" in result.stdout


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-specific")
def test_local_environment_timeout_terminates_the_entire_process_group(monkeypatch) -> None:
    signals: list[tuple[int, int]] = []

    async def scenario() -> CommandResult:
        terminated = asyncio.Event()

        class Process:
            pid = 12345
            returncode: int | None = None
            stdout = _PipeReader()
            stderr = _PipeReader()

            async def wait(self) -> int:
                if self.returncode is None:
                    await terminated.wait()
                assert self.returncode is not None
                return self.returncode

        process = Process()

        async def create_process(*_: object, **__: object) -> Process:
            return process

        def killpg(pid: int, sig: int) -> None:
            signals.append((pid, sig))
            process.returncode = -sig
            terminated.set()

        monkeypatch.setattr(
            environment_module.asyncio, "create_subprocess_shell", create_process
        )
        monkeypatch.setattr(environment_module.os, "killpg", killpg)
        return await LocalExecutionEnvironment().run_command(
            "long-running", timeout_seconds=1
        )

    result = asyncio.run(scenario())

    assert result.timed_out
    assert signals == [(12345, signal.SIGTERM)]


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-specific")
def test_local_environment_closes_background_children_that_hold_pipes(monkeypatch) -> None:
    signals: list[tuple[int, int]] = []

    async def scenario() -> CommandResult:
        close_pipes = asyncio.Event()

        class Reader:
            async def read(self, _: int) -> bytes:
                await close_pipes.wait()
                return b""

        class Process:
            pid = 24680
            returncode = 0
            stdout = Reader()
            stderr = Reader()

            async def wait(self) -> int:
                return self.returncode

        async def create_process(*_: object, **__: object) -> Process:
            return Process()

        def killpg(pid: int, sig: int) -> None:
            signals.append((pid, sig))
            close_pipes.set()

        monkeypatch.setattr(environment_module, "_OUTPUT_DRAIN_TIMEOUT", 0.01)
        monkeypatch.setattr(
            environment_module.asyncio, "create_subprocess_shell", create_process
        )
        monkeypatch.setattr(environment_module.os, "killpg", killpg)
        return await LocalExecutionEnvironment().run_command("background-child")

    result = asyncio.run(scenario())

    assert result.exit_code == 0
    assert result.stdout == "" and result.stderr == ""
    assert signals == [(24680, signal.SIGTERM)]


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-specific")
def test_local_environment_escalates_to_kill_for_a_stuck_process_group(monkeypatch) -> None:
    signals: list[tuple[int, int]] = []

    async def scenario() -> None:
        released = asyncio.Event()

        class Process:
            pid = 13579
            returncode: int | None = None

            async def wait(self) -> int:
                if self.returncode is None:
                    await released.wait()
                assert self.returncode is not None
                return self.returncode

        process = Process()

        def killpg(pid: int, sig: int) -> None:
            signals.append((pid, sig))
            if sig == signal.SIGKILL:
                process.returncode = -sig
                released.set()

        monkeypatch.setattr(environment_module, "_TERMINATION_GRACE_SECONDS", 0.01)
        monkeypatch.setattr(environment_module.os, "killpg", killpg)
        await environment_module._stop_process_group(process)

    asyncio.run(scenario())

    assert signals == [(13579, signal.SIGTERM), (13579, signal.SIGKILL)]


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-specific")
def test_local_environment_cancellation_terminates_the_process_group(monkeypatch) -> None:
    signals: list[tuple[int, int]] = []

    async def scenario() -> None:
        started = asyncio.Event()
        terminated = asyncio.Event()

        class Process:
            pid = 67890
            returncode: int | None = None
            stdout = _PipeReader()
            stderr = _PipeReader()

            async def wait(self) -> int:
                started.set()
                if self.returncode is None:
                    await terminated.wait()
                assert self.returncode is not None
                return self.returncode

        process = Process()

        async def create_process(*_: object, **__: object) -> Process:
            return process

        def killpg(pid: int, sig: int) -> None:
            signals.append((pid, sig))
            process.returncode = -sig
            terminated.set()

        monkeypatch.setattr(
            environment_module.asyncio, "create_subprocess_shell", create_process
        )
        monkeypatch.setattr(environment_module.os, "killpg", killpg)
        task = asyncio.create_task(
            LocalExecutionEnvironment().run_command("long-running")
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert signals == [(67890, signal.SIGTERM)]


def test_registry_injects_the_bound_environment() -> None:
    registry = ToolRegistry()

    async def probe(*, environment=None) -> str:
        return f"env={getattr(environment, 'name', None)}"

    registry.register(name="probe", description="", parameters={}, func=probe)

    result = asyncio.run(registry.execute(ToolCall(id="1", name="probe", arguments={})))

    assert result.content == "env=host"


def test_registry_injection_overrides_model_supplied_environment() -> None:
    registry = ToolRegistry()

    async def probe(*, environment=None) -> str:
        return f"env={getattr(environment, 'name', None)}"

    registry.register(name="probe", description="", parameters={}, func=probe)

    result = asyncio.run(
        registry.execute(
            ToolCall(id="1", name="probe", arguments={"environment": "forged"})
        )
    )

    assert result.content == "env=host"


@pytest.mark.parametrize("required_environment", ["sandbox", "either"])
def test_registry_rejects_non_host_tools_that_cannot_receive_the_bound_environment(
    required_environment: str,
) -> None:
    registry = ToolRegistry()

    async def bypasses_environment() -> str:
        return "ran directly on the host"

    with pytest.raises(ValueError, match="must accept an 'environment' parameter"):
        registry.register(
            name="unsafe",
            description="",
            parameters={},
            func=bypasses_environment,
            execution_environment=required_environment,
        )


def test_registry_blocks_tools_that_require_a_missing_sandbox() -> None:
    registry = ToolRegistry()
    calls: list[str] = []

    async def sandboxed(*, environment=None) -> str:
        calls.append("ran")
        return f"ran with {environment.name}"

    registry.register(
        name="sandboxed",
        description="",
        parameters={},
        func=sandboxed,
        execution_environment="sandbox",
    )

    result = asyncio.run(
        registry.execute(ToolCall(id="1", name="sandboxed", arguments={}))
    )

    assert result.is_error
    assert result.error_kind == "environment_unavailable"
    assert calls == []


def test_registry_allows_either_tools_under_any_environment() -> None:
    class FakeSandbox:
        name = "sandbox"

        async def run_command(
            self, command=None, *, argv=None, cwd=None, timeout_seconds=30.0, env=None
        ) -> CommandResult:
            return CommandResult(exit_code=0, stdout="sandboxed")

    registry = ToolRegistry()

    async def probe(*, environment=None) -> str:
        return f"env={environment.name}"

    registry.register(
        name="probe",
        description="",
        parameters={},
        func=probe,
        execution_environment="either",
    )
    registry.bind_execution_environment(FakeSandbox())

    result = asyncio.run(registry.execute(ToolCall(id="1", name="probe", arguments={})))

    assert result.content == "env=sandbox"


def test_registry_routes_host_and_sandbox_tools_to_their_declared_backends() -> None:
    class FakeSandbox:
        name = "sandbox"

        async def run_command(
            self, command=None, *, argv=None, cwd=None, timeout_seconds=30.0, env=None
        ) -> CommandResult:
            return CommandResult(exit_code=0)

    registry = ToolRegistry()

    async def probe(*, environment=None) -> str:
        return f"env={environment.name}"

    registry.register("host_probe", "", {}, probe, execution_environment="host")
    registry.register("sandbox_probe", "", {}, probe, execution_environment="sandbox")
    registry.bind_execution_environment(FakeSandbox())

    host = asyncio.run(registry.execute(ToolCall(id="host", name="host_probe")))
    sandbox = asyncio.run(
        registry.execute(ToolCall(id="sandbox", name="sandbox_probe"))
    )

    assert host.content == "env=host"
    assert sandbox.content == "env=sandbox"


def test_registry_authorization_never_bypasses_a_hard_safety_block(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class FakeSandbox:
        name = "sandbox"

        async def run_command(
            self, command=None, *, argv=None, cwd=None, timeout_seconds=30.0, env=None
        ) -> CommandResult:
            return CommandResult(exit_code=0)

    async def shell(*, command: str, cwd: str | None = None, environment=None) -> str:
        calls.append(command)
        return "ran"

    registry = ToolRegistry()
    registry.register(
        "shell",
        "",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
            },
        },
        shell,
        path_args=("cwd",),
        opaque_command=True,
        permission_level="execute",
        approval_policy="always",
        side_effect="external",
        execution_environment="sandbox",
    )
    registry.bind_working_directory(tmp_path)
    registry.bind_execution_environment(FakeSandbox())
    guard = ToolGuard(
        ROOT / "agents" / "safety" / "dangerous_commands.json",
        tool_registry=registry.definitions(),
    )
    guard.set_working_directory(tmp_path)
    registry.bind_tool_guard(guard)
    call = registry.normalize_call(
        ToolCall(id="blocked", name="shell", arguments={"command": "rm -rf /"})
    )

    async def run():
        with registry.authorize_execution(call):
            return await registry.execute(call)

    result = asyncio.run(run())

    assert result.is_error
    assert result.error_kind == "policy_blocked"
    assert calls == []


def test_registry_requires_an_approval_id_for_approval_gated_execution(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class FakeSandbox:
        name = "sandbox"

        async def run_command(
            self, command=None, *, argv=None, cwd=None, timeout_seconds=30.0, env=None
        ) -> CommandResult:
            return CommandResult(exit_code=0)

    async def shell(*, command: str, cwd: str | None = None, environment=None) -> str:
        calls.append(command)
        return "ran"

    registry = ToolRegistry()
    registry.register(
        "shell",
        "",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
            },
        },
        shell,
        path_args=("cwd",),
        opaque_command=True,
        permission_level="execute",
        approval_policy="always",
        side_effect="external",
        execution_environment="sandbox",
    )
    registry.bind_working_directory(tmp_path)
    registry.bind_execution_environment(FakeSandbox())
    guard = ToolGuard(
        ROOT / "agents" / "safety" / "dangerous_commands.json",
        tool_registry=registry.definitions(),
    )
    guard.set_working_directory(tmp_path)
    registry.bind_tool_guard(guard)
    call = registry.normalize_call(
        ToolCall(id="approval-required", name="shell", arguments={"command": "pwd"})
    )

    async def run():
        with registry.authorize_execution(call):
            return await registry.execute(call)

    result = asyncio.run(run())

    assert result.is_error
    assert result.error_kind == "approval_required"
    assert calls == []


def test_git_ops_runs_through_the_environment_end_to_end(tmp_path: Path) -> None:
    init = asyncio.run(
        LocalExecutionEnvironment().run_command(argv=["git", "init", str(tmp_path)])
    )
    assert init.exit_code == 0, init.error or init.stderr

    registry = ToolRegistry()
    registry.load_providers(ROOT / "agents" / "tools")
    result = asyncio.run(
        registry.execute(
            ToolCall(
                id="1",
                name="git_ops",
                arguments={"action": "status", "cwd": str(tmp_path)},
            )
        )
    )

    assert not result.is_error, result.content
    assert result.content.startswith("[exit_code=0]")
