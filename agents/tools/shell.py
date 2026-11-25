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
import re

TOOL_META = {
    "name": "shell",
    "description": (
        "Execute a sandboxed shell command from the project directory. Every command requires "
        "user approval and receives a minimal, credential-free environment. "
        "Outbound network access is disabled by default (loopback-only); set network=true "
        "only when external network access is genuinely required."
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
            },
            "network": {
                "type": "boolean",
                "description": (
                    "Request outbound network access for this command (default false: loopback-only). "
                    "Set true ONLY when external network access is genuinely required; triggers HIGH-risk approval."
                ),
                "default": False,
            },
        },
        "required": ["command"]
    },
    "path_args": ["cwd"],
    "opaque_command": True,
    "accepts_vault_placeholders": False,
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
_OFFLINE_ERROR_RE = re.compile(
    r"(?i)("
    r"network is unreachable"
    r"|enetunreach"
    r"|eai_again"
    r"|temporary failure in name resolution"
    r"|name or service not known"
    r"|nodename nor servname provided"
    r"|could not resolve host"
    r"|failed to establish a new connection"
    r"|connecterror"
    r"|failed to download"
    r"|offline mode"
    r")"
)
_EPERM_NETWORK_CONTEXT_RE = re.compile(
    r"(?i)\b(connect|socket|http|https|curl|fetch|resolve|dns|tcp|udp|ssl|tls)\b"
)
_NETWORK_DENIED_HINT = (
    "[sandbox:network_denied]\n"
    "The command ran in loopback-only mode (network=false) and appears to have failed "
    "due to blocked outbound network access.\n"
    "If external network access is genuinely required, retry `shell` with `network=true` "
    "(this will request explicit HIGH-risk user approval)."
)


def _looks_like_network_denied(stdout: str, stderr: str) -> bool:
    combined = f"{stderr}\n{stdout}"
    if _OFFLINE_ERROR_RE.search(combined):
        return True
    return (
        "operation not permitted" in combined.lower()
        and _EPERM_NETWORK_CONTEXT_RE.search(combined) is not None
    )


def _safe_environment(cwd: str | None) -> dict[str, str]:
    """Return the minimal environment a model-requested shell may inherit.

    Provider credentials and other service secrets must never be exposed to a
    command string.  Pointing ``HOME`` at the project also prevents accidental
    reads of user-level config and credential files through shell expansion.

    This is a *fallback* for host execution.  Under an OS sandbox (macOS
    Seatbelt or Linux bubblewrap), the bound backend constructs the child
    environment from ``ResolvedSandboxProfile`` (validated toolchains, cache
    roots, and devnull config pins) and copies through only ``_SAFE_ENV_KEYS``.
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
    *,
    command: str,
    timeout: int = 30,
    cwd: str | None = None,
    network: bool = False,
    environment=None,
) -> str:
    if not isinstance(command, str) or not command.strip():
        return "Error: command must be a non-empty string"
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return "Error: timeout must be a number"
    if cwd is not None and not isinstance(cwd, str):
        return "Error: cwd must be a string"
    if not isinstance(network, bool):
        return "Error: network must be a boolean"
    timeout = min(max(1, timeout), MAX_TIMEOUT)

    if cwd and not os.path.isdir(cwd):
        return f"Error: working directory does not exist: {cwd}"
    if environment is None:
        return "Error: no execution environment is available for shell"

    try:
        result = await environment.run_command(
            command,
            cwd=cwd,
            timeout_seconds=timeout,
            env=_safe_environment(cwd),
            network=network,
        )
    except TypeError:
        # Backwards compatibility for custom/test ExecutionEnvironment stubs
        # that have not added the optional `network` keyword argument yet.
        if network:
            return "Error: bound execution environment does not support network gating"
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
        output_parts.append(
            "[warning] output may be incomplete: a detached background process "
            "still holds the output pipes"
        )
    if (
        not network
        and result.exit_code not in (None, 0)
        and (
            getattr(result, "network_denied_hint", False)
            or _looks_like_network_denied(result.stdout, result.stderr)
        )
    ):
        output_parts.append(_NETWORK_DENIED_HINT)

    body = "\n".join(output_parts) if output_parts else "(no output)"
    return f"[exit_code={result.exit_code}]\n{body}"
