"""Host-process execution backend shared by sandbox adapters.

This is deliberately the only module that owns subprocess lifecycle
management.  A sandbox adapter transforms a command into a constrained
process invocation, then delegates here for cancellation, timeout, output
limits, and cleanup.
"""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

MAX_OUTPUT = 10 * 1024
_STREAM_CHUNK_SIZE = 4096
_OUTPUT_DRAIN_TIMEOUT = 1.0
_TERMINATION_GRACE_SECONDS = 1.0


@dataclass
class CommandResult:
    """Outcome of one command executed by an execution backend."""

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None
    # The command ran and exit_code/stdout/stderr are real, but a detached
    # process kept the pipes open so reading was abandoned.  Deliberately not
    # folded into ``error``: callers treat that as "the command failed" and
    # discard the exit code and output along with it.
    output_incomplete: bool = False


@runtime_checkable
class ExecutionEnvironment(Protocol):
    """Boundary through which tools turn commands into OS processes."""

    name: str

    async def run_command(
        self,
        command: str | None = None,
        *,
        argv: list[str] | None = None,
        cwd: str | None = None,
        timeout_seconds: float = 30.0,
        env: dict[str, str] | None = None,
    ) -> CommandResult: ...


@dataclass
class _StreamBuffer:
    """Live view of one stream, readable after its reader task is abandoned.

    Tracks retained bytes and the real byte count separately: the abandoned-drain
    path needs the true total, or ``_format_stream`` silently omits its
    "truncated, N bytes total" note precisely when output *was* dropped.
    """

    chunks: list[bytes] = field(default_factory=list)
    total: int = 0

    def value(self) -> tuple[bytes, int]:
        return b"".join(self.chunks), self.total


async def _read_limited(
    stream: asyncio.StreamReader, buffer: _StreamBuffer | None = None
) -> tuple[bytes, int]:
    """Read a stream to EOF, retaining at most ``MAX_OUTPUT`` bytes.

    ``buffer`` accumulates as data arrives, so a caller forced to abandon this
    task on timeout can still recover what was read and how much was produced.
    """
    sink = buffer if buffer is not None else _StreamBuffer()
    retained = 0
    while chunk := await stream.read(_STREAM_CHUNK_SIZE):
        sink.total += len(chunk)
        remaining = MAX_OUTPUT - retained
        if remaining > 0:
            sink.chunks.append(chunk[:remaining])
            retained += min(len(chunk), remaining)
    return sink.value()


def _format_stream(data: bytes, total: int) -> str:
    text = data.decode("utf-8", errors="replace")
    return text if total <= len(data) else f"{text}\n... (truncated, {total} bytes total)"


def _signal_process_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, sig)
        elif sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
    except ProcessLookupError:
        pass


async def _stop_process_group(proc: asyncio.subprocess.Process) -> None:
    _signal_process_group(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=_TERMINATION_GRACE_SECONDS)
    except asyncio.TimeoutError:
        _signal_process_group(proc, signal.SIGKILL)
        await proc.wait()


async def _drain_streams(
    proc: asyncio.subprocess.Process,
    stdout_task: asyncio.Task[tuple[bytes, int]],
    stderr_task: asyncio.Task[tuple[bytes, int]],
    stdout_buffer: _StreamBuffer | None = None,
    stderr_buffer: _StreamBuffer | None = None,
) -> tuple[tuple[bytes, int], tuple[bytes, int], bool]:
    """Drain both streams, giving up rather than waiting on a stalled pipe.

    Returns ``(stdout, stderr, complete)``.  The final wait used to be unbounded,
    which hung forever whenever the command spawned a grandchild that called
    ``setsid()`` and inherited the pipes: ``killpg`` cannot reach it, so EOF never
    arrives.  Because the shell tool is declared ``concurrency: "serial"``, that
    left its lock held and every later shell call queued behind it.
    """
    drain_task = asyncio.gather(stdout_task, stderr_task)
    try:
        stdout, stderr = await asyncio.wait_for(
            asyncio.shield(drain_task), _OUTPUT_DRAIN_TIMEOUT
        )
        return stdout, stderr, True
    except asyncio.TimeoutError:
        _signal_process_group(proc, signal.SIGTERM)
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(drain_task), _TERMINATION_GRACE_SECONDS
            )
            return stdout, stderr, True
        except asyncio.TimeoutError:
            _signal_process_group(proc, signal.SIGKILL)
            try:
                stdout, stderr = await asyncio.wait_for(
                    asyncio.shield(drain_task), _TERMINATION_GRACE_SECONDS
                )
                return stdout, stderr, True
            except asyncio.TimeoutError:
                # A detached grandchild still holds the pipes.  Abandon the read
                # and surface what we have; blocking here is what wedged the tool.
                await _cancel_stream_tasks(stdout_task, stderr_task)
                empty = _StreamBuffer()
                return (
                    (stdout_buffer or empty).value(),
                    (stderr_buffer or empty).value(),
                    False,
                )


async def _cancel_stream_tasks(
    stdout_task: asyncio.Task[tuple[bytes, int]] | None,
    stderr_task: asyncio.Task[tuple[bytes, int]] | None,
) -> None:
    tasks = [task for task in (stdout_task, stderr_task) if task is not None]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class LocalExecutionEnvironment:
    """Explicit host backend, with hardened process handling but no isolation."""

    name = "host"

    async def run_command(
        self,
        command: str | None = None,
        *,
        argv: list[str] | None = None,
        cwd: str | None = None,
        timeout_seconds: float = 30.0,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        if (command is None) == (argv is None):
            return CommandResult(exit_code=None, error="exactly one of command or argv is required")

        proc: asyncio.subprocess.Process | None = None
        stdout_task: asyncio.Task[tuple[bytes, int]] | None = None
        stderr_task: asyncio.Task[tuple[bytes, int]] | None = None
        # Live buffers, so output already read survives an abandoned drain.
        stdout_buffer = _StreamBuffer()
        stderr_buffer = _StreamBuffer()
        try:
            options: dict[str, object] = {
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
                "cwd": cwd,
                "env": env,
            }
            if os.name == "posix":
                options["start_new_session"] = True
            if command is not None:
                proc = await asyncio.create_subprocess_shell(command, **options)
            else:
                assert argv is not None
                proc = await asyncio.create_subprocess_exec(*argv, **options)
            assert proc.stdout is not None and proc.stderr is not None
            stdout_task = asyncio.create_task(_read_limited(proc.stdout, stdout_buffer))
            stderr_task = asyncio.create_task(_read_limited(proc.stderr, stderr_buffer))
            await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
        except FileNotFoundError:
            binary = argv[0] if argv else str(command)
            return CommandResult(exit_code=None, error=f"{binary} is not installed or not in PATH")
        except asyncio.TimeoutError:
            if proc is not None:
                await _stop_process_group(proc)
            await _cancel_stream_tasks(stdout_task, stderr_task)
            return CommandResult(exit_code=None, timed_out=True)
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                await _stop_process_group(proc)
            await _cancel_stream_tasks(stdout_task, stderr_task)
            raise
        except Exception as exc:
            if proc is not None and proc.returncode is None:
                await _stop_process_group(proc)
            await _cancel_stream_tasks(stdout_task, stderr_task)
            return CommandResult(exit_code=None, error=str(exc))

        assert proc is not None and stdout_task is not None and stderr_task is not None
        (stdout, stdout_total), (stderr, stderr_total), drained = await _drain_streams(
            proc, stdout_task, stderr_task, stdout_buffer, stderr_buffer
        )
        return CommandResult(
            exit_code=proc.returncode,
            stdout=_format_stream(stdout, stdout_total) if stdout else "",
            stderr=_format_stream(stderr, stderr_total) if stderr else "",
            output_incomplete=not drained,
        )
