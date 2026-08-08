"""Regression tests for the privileged raw-shell execution boundary.

Process-lifecycle tests (timeout, process groups, cancellation) live in
test_execution_environment.py since process handling moved into the engine's
LocalExecutionEnvironment; these tests cover the shell provider's own
promises: approval, metadata, credential stripping, and output formatting.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path

from engine.safety.tool_guard import ToolGuard
from engine.sandbox import host as environment_module
from engine.sandbox import MAX_OUTPUT, LocalExecutionEnvironment
from engine.tool.interface import ToolCall
from engine.tool.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[3]
RULES = ROOT / "agents" / "safety" / "dangerous_commands.json"
def _load_shell_provider():
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module("agents.tools.shell")


def test_raw_shell_always_requires_user_approval(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    guard = ToolGuard(RULES, allowed_dirs=[project])

    for command in (
        "cat ../outside.txt",
        "cat $PWD/../outside.txt",
        "echo $OPENAI_API_KEY",
        "printf ok | tee ../outside.txt",
    ):
        result = guard.check(
            ToolCall(
                id=command,
                name="shell",
                arguments={"command": command, "cwd": str(project)},
            )
        )
        assert result.allowed, command
        assert result.approval_required, command


def test_shell_definition_records_external_side_effects() -> None:
    registry = ToolRegistry()
    registry.load_providers(ROOT / "agents" / "tools")

    definition = registry.definitions()["shell"]
    assert definition.permission_level == "execute"
    assert definition.approval_policy == "always"
    assert definition.side_effect == "external"
    assert not definition.idempotent


def test_shell_requires_an_execution_environment(tmp_path: Path) -> None:
    shell = _load_shell_provider()

    result = asyncio.run(shell.execute(command="echo hi", cwd=str(tmp_path)))

    assert result == "Error: no execution environment is available for shell"


def test_shell_strips_inherited_secrets_and_caps_streamed_output(
    monkeypatch, tmp_path: Path
) -> None:
    shell = _load_shell_provider()
    captured: dict[str, object] = {}

    class Reader:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks

        async def read(self, _: int) -> bytes:
            return self._chunks.pop(0) if self._chunks else b""

    # A test runner puts its verdict on the last line, so the cap must retain
    # both ends: a head-only cap dropped exactly the evidence the TDD and
    # review gates require.
    head_marker = b"HEAD-collection-error\n"
    tail_marker = b"\nTAIL-VERDICT: 3 failed"
    filler = b"x" * (MAX_OUTPUT * 2)
    total_bytes = len(head_marker) + len(filler) + len(tail_marker)

    class Process:
        pid = 12345
        returncode = 0
        stdout = Reader([head_marker, filler, tail_marker])
        stderr = Reader([])

        async def wait(self) -> int:
            return self.returncode

    async def create_process(command: str, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return Process()

    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setattr(
        environment_module.asyncio, "create_subprocess_shell", create_process
    )

    result = asyncio.run(
        shell.execute(
            command="echo ok",
            cwd=str(tmp_path),
            environment=LocalExecutionEnvironment(),
        )
    )

    environment = captured["env"]
    assert isinstance(environment, dict)
    assert "OPENAI_API_KEY" not in environment
    assert environment["HOME"] == str(tmp_path.resolve())
    assert captured["cwd"] == str(tmp_path)
    if os.name == "posix":
        assert captured["start_new_session"] is True
    assert head_marker.decode() in result
    assert tail_marker.decode() in result, "the tail carries the verdict and must survive"
    assert "middle truncated" in result
    assert f"{total_bytes} bytes total" in result


def test_shell_process_cannot_read_the_service_secret_environment(
    monkeypatch, tmp_path: Path
) -> None:
    shell = _load_shell_provider()
    monkeypatch.setenv("OPENAI_API_KEY", "shell-test-secret")

    result = asyncio.run(
        shell.execute(
            command='printf "%s" "$OPENAI_API_KEY"',
            cwd=str(tmp_path),
            environment=LocalExecutionEnvironment(),
        )
    )

    assert "shell-test-secret" not in result
    assert result == "[exit_code=0]\n(no output)"


def test_shell_preserves_small_stderr_and_rejects_missing_workdir(tmp_path: Path) -> None:
    shell = _load_shell_provider()
    env = LocalExecutionEnvironment()

    stderr_result = asyncio.run(
        shell.execute(command="printf failure >&2", cwd=str(tmp_path), environment=env)
    )
    missing_result = asyncio.run(
        shell.execute(command="pwd", cwd=str(tmp_path / "missing"), environment=env)
    )

    assert stderr_result == "[exit_code=0]\n[stderr]\nfailure"
    assert missing_result == f"Error: working directory does not exist: {tmp_path / 'missing'}"


def test_shell_timeout_reports_the_clamped_timeout(tmp_path: Path) -> None:
    shell = _load_shell_provider()

    result = asyncio.run(
        shell.execute(
            command="sleep 30",
            timeout=1,
            cwd=str(tmp_path),
            environment=LocalExecutionEnvironment(),
        )
    )

    assert result == "Error: command timed out after 1s"
