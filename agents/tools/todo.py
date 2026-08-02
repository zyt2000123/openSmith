"""Todo tool — track tasks during a session."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

TOOL_META = {
    "name": "todo",
    "description": (
        "Manage a task list for the current session. Use to track multi-step work, "
        "show progress to the user, and organize complex tasks. "
        "Actions: list, add, update, remove, clear."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "add", "update", "remove", "clear"],
                "description": "Action to perform",
            },
            "text": {"type": "string", "description": "Task description (for add)"},
            "index": {"type": "integer", "description": "Task index (for update/remove, 1-based)"},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "done"],
                "description": "New status (for update)",
            },
        },
        "required": ["action"],
    },
    "permission_level": "write",
    "approval_policy": "policy",
    "side_effect": "write",
    "concurrency": "serial",
    "execution_environment": "host",
}

_ICONS = {"pending": "○", "in_progress": "◐", "done": "●"}


def _ensure_private_dir(path: Path) -> None:
    """Create *path* and every missing ancestor with 0700.

    ``mkdir(parents=True, mode=...)`` applies the mode only to the final
    component and lets created ancestors fall back to the process umask; only
    directories this function actually creates are tightened, so an existing
    shared ancestor is left alone.
    """
    missing: list[Path] = []
    probe = path
    while not probe.exists() and probe.parent != probe:
        missing.append(probe)
        probe = probe.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True, mode=0o700)
        directory.chmod(0o700)


def _load_todos(todo_file: str | Path) -> list[dict] | None:
    path = Path(todo_file)
    if not path.is_file():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, list):
        return None
    todos: list[dict] = []
    for entry in loaded:
        if not isinstance(entry, dict):
            return None
        text = entry.get("text")
        status = entry.get("status")
        if not isinstance(text, str) or status not in _ICONS:
            return None
        todos.append({"text": text, "status": status})
    return todos


def _save_todos(todos: list[dict], todo_file: str | Path) -> None:
    path = Path(todo_file)
    _ensure_private_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(todos, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _fmt(todos: list[dict]) -> str:
    if not todos:
        return "No tasks."
    lines = [f"{i}. {_ICONS.get(t['status'],'?')} [{t['status']}] {t['text']}" for i, t in enumerate(todos, 1)]
    done = sum(1 for t in todos if t["status"] == "done")
    lines.append(f"\nProgress: {done}/{len(todos)} done")
    return "\n".join(lines)


def _execute_sync(
    *, action: str, text: str = "", index: int = 0, status: str = "pending",
    todo_file: str | Path | None = None,
) -> str:
    if todo_file is None:
        return "Error: todo runtime storage was not provided by the engine"
    if not isinstance(action, str):
        return "Error: action must be a string"
    if isinstance(index, bool) or not isinstance(index, int):
        return "Error: index must be an integer"
    todos = _load_todos(todo_file)
    if todos is None:
        return "Error: Todo state is invalid; refusing to overwrite it"
    if action == "list":
        return _fmt(todos)
    if action == "add":
        if not text.strip():
            return "Error: text is required for add"
        todos.append({"text": text.strip(), "status": "pending"})
        _save_todos(todos, todo_file)
        return f"Added task {len(todos)}: {text.strip()}\n\n{_fmt(todos)}"
    if action == "update":
        if index < 1 or index > len(todos):
            return f"Error: invalid index {index}. Tasks: 1-{len(todos)}"
        if status not in _ICONS:
            return f"Error: invalid status '{status}'"
        todos[index - 1]["status"] = status
        if text.strip():
            todos[index - 1]["text"] = text.strip()
        _save_todos(todos, todo_file)
        return f"Updated task {index}.\n\n{_fmt(todos)}"
    if action == "remove":
        if index < 1 or index > len(todos):
            return f"Error: invalid index {index}. Tasks: 1-{len(todos)}"
        removed = todos.pop(index - 1)
        _save_todos(todos, todo_file)
        return f"Removed: {removed['text']}\n\n{_fmt(todos)}"
    if action == "clear":
        n = len(todos)
        todos.clear()
        _save_todos(todos, todo_file)
        return f"Cleared {n} task(s)."
    return f"Error: unknown action '{action}'"


async def execute(
    *, action: str, text: str = "", index: int = 0, status: str = "pending",
    todo_file: str | Path | None = None,
) -> str:
    return await asyncio.to_thread(
        _execute_sync,
        action=action,
        text=text,
        index=index,
        status=status,
        todo_file=todo_file,
    )
