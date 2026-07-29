"""Write file tool provider — writes content to a file within the work directory."""

import asyncio
import os
from collections.abc import Callable

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


def _is_within_workdir(path: str, work_dir: str) -> bool:
    resolved = os.path.realpath(path)
    work_resolved = os.path.realpath(work_dir)
    return resolved.startswith(work_resolved + os.sep) or resolved == work_resolved


def _execute_sync(
    *,
    path: str,
    content: str,
    append: bool = False,
    _work_dir: str = "",
    _snapshot_tracker: Callable[[str], object] | None = None,
) -> str:
    if _work_dir and not _is_within_workdir(path, _work_dir):
        return (
            f"Error: path '{path}' is outside the allowed work directory '{_work_dir}'"
        )

    resolved = os.path.realpath(path) if os.path.isabs(path) else os.path.abspath(path)

    parent = os.path.dirname(resolved)
    if not os.path.exists(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return f"Error: cannot create directory {parent}: {e}"

    # The engine injects this runtime capability per session. Content stays
    # portable and never imports engine internals.
    if not append and _snapshot_tracker is not None:
        try:
            _snapshot_tracker(resolved)
        except Exception:
            pass

    mode = "a" if append else "w"
    try:
        with open(resolved, mode, encoding="utf-8") as f:
            f.write(content)
    except PermissionError:
        return f"Error: permission denied: {resolved}"
    except Exception as e:
        return f"Error writing file: {e}"

    action = "appended to" if append else "wrote"
    size = len(content.encode("utf-8"))
    return f"OK: {action} {resolved} ({size} bytes)"


async def execute(
    *,
    path: str,
    content: str,
    append: bool = False,
    _work_dir: str = "",
    _snapshot_tracker: Callable[[str], object] | None = None,
) -> str:
    return await asyncio.to_thread(
        _execute_sync,
        path=path,
        content=content,
        append=append,
        _work_dir=_work_dir,
        _snapshot_tracker=_snapshot_tracker,
    )
