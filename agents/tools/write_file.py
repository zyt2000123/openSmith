"""Write file tool provider — writes content to a file within the work directory."""
# 写入被限定在工作目录内，并由上层安全守卫统一处理路径与审批。

import asyncio
import logging
import os
from collections.abc import Callable

log = logging.getLogger(__name__)

TOOL_META = {
    "name": "write_file",
    "description": "Write content to a local file. Creates parent directories if needed.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to write (must be within work directory)"
            },
            "content": {
                "type": "string",
                "description": "Content to write to the file"
            },
            "append": {
                "type": "boolean",
                "description": "If true, append to existing file instead of overwriting",
                "default": False
            }
        },
        "required": ["path", "content"]
    },
    "path_args": ["path"],
    "is_write_tool": True,
    "permission_level": "write",
    "approval_policy": "policy",
    "side_effect": "write",
    "concurrency": "serial",
    "execution_environment": "host",
}


MAX_WRITE_BYTES = 8 * 1024 * 1024  # 8MB per call


def _execute_sync(
    *,
    path: str,
    content: str,
    append: bool = False,
    _snapshot_tracker: Callable[[str], object] | None = None,
) -> str:
    if not isinstance(content, str):
        return "Error: content must be a string"
    size = len(content.encode("utf-8"))
    if size > MAX_WRITE_BYTES:
        return (
            f"Error: content exceeds the {MAX_WRITE_BYTES // (1024 * 1024)} MB "
            "per-call write limit"
        )
    # The directory boundary lives in engine/safety/tool_guard.py, which runs
    # before this provider and is the only layer that knows about approvals — a
    # user may approve a write outside the workspace, and a provider-level block
    # would silently defeat that.  A second check here also read its root from
    # the same model-supplied argument dict it was meant to constrain, so it
    # could never have been authoritative.
    resolved = os.path.realpath(path) if os.path.isabs(path) else os.path.abspath(path)

    parent = os.path.dirname(resolved)
    if not os.path.exists(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return f"Error: cannot create directory {parent}: {e}"

    # The engine injects this runtime capability per session. Content stays
    # portable and never imports engine internals.
    #
    # A failed snapshot means the overwritten content is unrecoverable.  The
    # write still proceeds — the engine owns undo policy — but swallowing the
    # failure reported plain success with no trace anywhere that undo is gone.
    snapshot_warning = ""
    if not append and _snapshot_tracker is not None:
        try:
            if _snapshot_tracker(resolved) is False:
                snapshot_warning = " [warning] no undo snapshot was recorded"
        except Exception as e:
            log.warning("snapshot failed for %s: %s", resolved, e, exc_info=True)
            snapshot_warning = (
                f" [warning] no undo snapshot was recorded ({type(e).__name__})"
            )

    mode = "a" if append else "w"
    try:
        with open(resolved, mode, encoding="utf-8") as f:
            f.write(content)
    except PermissionError:
        return f"Error: permission denied: {resolved}"
    except Exception as e:
        return f"Error writing file: {e}"

    action = "appended to" if append else "wrote"
    return f"OK: {action} {resolved} ({size} bytes){snapshot_warning}"


async def execute(
    *,
    path: str,
    content: str,
    append: bool = False,
    _snapshot_tracker: Callable[[str], object] | None = None,
) -> str:
    return await asyncio.to_thread(
        _execute_sync,
        path=path,
        content=content,
        append=append,
        _snapshot_tracker=_snapshot_tracker,
    )
