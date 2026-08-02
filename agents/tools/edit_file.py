"""Edit file tool — precise string replacement without full rewrite."""

import asyncio
import logging
import os
from collections.abc import Callable

log = logging.getLogger(__name__)

TOOL_META = {
    "name": "edit_file",
    "description": (
        "Replace a specific string in a file. The old_string must appear exactly once "
        "in the file (unless replace_all is true). Use read_file first to see the exact content. "
        "For new files or full rewrites, use write_file instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to edit"},
            "old_string": {"type": "string", "description": "Exact text to find and replace"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences instead of requiring uniqueness",
                "default": False,
            },
        },
        "required": ["path", "old_string", "new_string"],
    },
    "path_args": ["path"],
    "is_write_tool": True,
    "permission_level": "write",
    "approval_policy": "policy",
    "side_effect": "write",
    "concurrency": "serial",
    "execution_environment": "host",
}


MAX_EDIT_BYTES = 8 * 1024 * 1024  # 8MB file / result cap


def _execute_sync(
    *, path: str, old_string: str, new_string: str, replace_all: bool = False,
    _snapshot_tracker: Callable[[str], object] | None = None,
) -> str:
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return "Error: old_string and new_string must be strings"
    if not old_string:
        # str.replace("", x) splices x between every character; replace_all on an
        # empty needle silently mangles the whole file.
        return "Error: old_string must not be empty"
    if old_string == new_string:
        return "Error: new_string must differ from old_string"

    # The directory boundary lives in engine/safety/tool_guard.py, which runs
    # before this provider and is the only layer that knows about approvals — a
    # user may approve an edit outside the workspace, and a provider-level block
    # would silently defeat that.  A second check here also read its root from
    # the same model-supplied argument dict it was meant to constrain, so it
    # could never have been authoritative.
    resolved = os.path.realpath(path) if os.path.isabs(path) else os.path.abspath(path)

    if not os.path.isfile(resolved):
        return f"Error: file not found: {resolved}"

    try:
        if os.path.getsize(resolved) > MAX_EDIT_BYTES:
            return (
                f"Error: file exceeds the {MAX_EDIT_BYTES // (1024 * 1024)} MB "
                "edit limit"
            )
    except OSError:
        return f"Error: cannot inspect file: {resolved}"

    try:
        # Strict decoding, never errors="replace": a lossy read followed by a
        # plain write silently corrupts the bytes that failed to decode.
        with open(resolved, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        return (
            f"Error: {resolved} is not valid UTF-8 text; edit_file only supports "
            "UTF-8 text files"
        )
    except PermissionError:
        return f"Error: permission denied: {resolved}"

    count = content.count(old_string)
    if count == 0:
        return (
            f"Error: old_string not found in {resolved}. "
            "Make sure you copied the exact text including whitespace and indentation."
        )

    if count > 1 and not replace_all:
        return (
            f"Error: old_string appears {count} times in {resolved}. "
            "Provide more surrounding context to make it unique, or set replace_all=true."
        )

    # A failed pre-edit snapshot means undo is gone.  The edit still proceeds —
    # the caller asked for it and the engine owns undo policy — but swallowing
    # the failure reported plain success while the original content became
    # unrecoverable, with no trace in the result or in any log.
    snapshot_warning = ""
    if _snapshot_tracker is not None:
        try:
            if _snapshot_tracker(resolved) is False:
                snapshot_warning = " [warning] no undo snapshot was recorded"
        except Exception as e:
            log.warning("snapshot failed for %s: %s", resolved, e, exc_info=True)
            snapshot_warning = (
                f" [warning] no undo snapshot was recorded ({type(e).__name__})"
            )

    updated = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
    if len(updated.encode("utf-8")) > MAX_EDIT_BYTES:
        return (
            f"Error: edited content exceeds the {MAX_EDIT_BYTES // (1024 * 1024)} MB "
            "edit limit"
        )

    try:
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(updated)
    except PermissionError:
        return f"Error: permission denied writing: {resolved}"

    replacements = count if replace_all else 1
    return (
        f"OK: edited {resolved} "
        f"({replacements} replacement{'s' if replacements > 1 else ''}){snapshot_warning}"
    )


async def execute(
    *, path: str, old_string: str, new_string: str, replace_all: bool = False,
    _snapshot_tracker: Callable[[str], object] | None = None,
) -> str:
    return await asyncio.to_thread(
        _execute_sync,
        path=path,
        old_string=old_string,
        new_string=new_string,
        replace_all=replace_all,
        _snapshot_tracker=_snapshot_tracker,
    )
