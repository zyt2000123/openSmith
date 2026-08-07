"""Shell command tool provider — runs commands via the bound execution environment.

Process handling (spawn, timeout, process-group termination, output capping)
lives in the engine's execution environment, which also owns the child
environment on the sandboxed path this tool declares; the dict built here is a
host-execution fallback. This provider validates arguments and formats results.
The ``environment`` argument is injected by the tool registry and is duck-typed
so this content-layer module never imports engine code.
"""
# Provider 不管理进程；沙箱路径下子进程环境也由 engine 决定，此处仅为宿主执行兜底。

from __future__ import annotations

import os

TOOL_META = {
    "name": "shell",
    "description": (
        "Execute a sandboxed shell command from the project directory. Every command requires "
        "user approval and receives a minimal, credential-free environment."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute"
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 30, max 600)",
                "default": 30
            },
            "cwd": {
                "type": "string",
                "description": "Working directory for command execution"
            }
        },
        "required": ["command"]
    },
    "path_args": ["cwd"],
    "opaque_command": True,
    "permission_level": "execute",
    "approval_policy": "always",
    "side_effect": "external",
    "idempotent": False,
    "concurrency": "serial",
    "execution_environment": "sandbox",
}

# A full project test suite is the evidence the TDD and review gates demand, so
# the ceiling has to clear one; 120s did not.
MAX_TIMEOUT = 600
_SAFE_ENV_KEYS = ("LANG", "LC_ALL", "TERM", "TZ", "NO_COLOR")


def _safe_environment(cwd: str | None) -> dict[str, str]:
    """Return the minimal environment a model-requested shell may inherit.

    Provider credentials and other service secrets must never be exposed to a
    command string.  Pointing ``HOME`` at the project also prevents accidental
    reads of user-level config and credential files through shell expansion.

    This is a *fallback*, not the operative policy.  The tool declares
    ``execution_environment: "sandbox"``, and the bound backend builds the child
    environment itself: the macOS Seatbelt adapter constructs ``PATH`` (from
    ``os.defpath``), ``HOME``, ``TMPDIR`` and the same devnull config pins from
    scratch, then copies through only ``_SAFE_ENV_KEYS``.  Everything else here
    is discarded on that path.  Keep the two in agreement rather than assuming
    this dict decides anything.

    A package-manager cache root used to be set here too, with a comment
    claiming it stopped ``uv run pytest`` from re-downloading dependencies into
    the project on every call.  It never reached a process — the sandbox dropped
    those keys — so that problem is still open, and it cannot be closed from
    here: the Seatbelt profile denies reads outside the workspace (a toolchain
    in ``/opt/homebrew/bin`` cannot be executed at all) and denies writes
    outside it (a cache under ``~/.cache`` cannot be written).  Closing it needs
    a deliberate profile decision.
    """
    home = os.path.abspath(cwd) if cwd else os.getcwd()
    environment = {
        "PATH": os.environ.get("PATH") or os.defpath,
        "HOME": home,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "PIP_CONFIG_FILE": os.devnull,
        "NPM_CONFIG_USERCONFIG": os.devnull,
    }
    for key in _SAFE_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment


async def execute(
    *, command: str, timeout: int = 30, cwd: str | None = None, environment=None
) -> str:
    if not isinstance(command, str) or not command.strip():
        return "Error: command must be a non-empty string"
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return "Error: timeout must be a number"
    if cwd is not None and not isinstance(cwd, str):
        return "Error: cwd must be a string"
    timeout = min(max(1, timeout), MAX_TIMEOUT)

    if cwd and not os.path.isdir(cwd):
        return f"Error: working directory does not exist: {cwd}"
    if environment is None:
        return "Error: no execution environment is available for shell"

    result = await environment.run_command(
        command,
        cwd=cwd,
        timeout_seconds=timeout,
        env=_safe_environment(cwd),
    )
    if result.timed_out:
        return f"Error: command timed out after {timeout}s"
    if result.error:
        return f"Error executing command: {result.error}"

    output_parts = []
    if result.stdout:
        output_parts.append(result.stdout)
    if result.stderr:
        output_parts.append(f"[stderr]\n{result.stderr}")
    if result.output_incomplete:
        # The command itself ran to completion; only reading its output was cut
        # short.  Report that alongside the real exit code rather than as an
        # execution failure, which would discard both exit code and output.
        output_parts.append(
            "[warning] output may be incomplete: a detached background process "
            "still holds the output pipes"
        )

    body = "\n".join(output_parts) if output_parts else "(no output)"
    return f"[exit_code={result.exit_code}]\n{body}"
