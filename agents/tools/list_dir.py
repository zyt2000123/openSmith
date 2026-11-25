"""List directory tool — tree-style directory listing."""
# 以受限深度和条目数展示目录结构，适合先建立项目全貌。

import asyncio
import os
import re
from pathlib import Path

TOOL_META = {
    "name": "list_dir",
    "description": (
        "List files and directories in a path. Returns a tree-style view. "
        "Use to understand project structure before reading specific files."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to list", "default": "."},
            "max_depth": {"type": "integer", "description": "Max directory depth (1-5)", "default": 2},
        },
        "required": [],
    },
    "path_args": ["path"],
    "permission_level": "read",
    "approval_policy": "never",
    "side_effect": "none",
    "execution_environment": "host",
}

MAX_ENTRIES = 300
EXCLUDED = {".git", "node_modules", "__pycache__", ".venv", "dist", ".build", ".DS_Store", ".egg-info"}

# Credential-bearing names a *directory* walk must never surface — not their
# contents, not even their existence and size.  ToolGuard gates a directly-named
# sensitive file (engine/safety/tool_guard.py `_is_sensitive_read_name`), but a
# recursive listing names a directory and reaches everything beneath it, so this
# tool has to exclude them itself.  Kept in sync with that guard predicate by
# engine/tests/tool/test_read_tool_secret_parity.py; `agents/` cannot import the
# engine, so the logic is mirrored, not shared.
_SECRET_DIRS = {".ssh", ".gnupg", ".aws", ".kube", ".docker"}
_SECRET_FILES = {".npmrc", ".pypirc", ".netrc", ".git-credentials"}
_SECRET_PATH_PAIRS = {
    (".config", "gh"),
    (".config", "gcloud"),
    ("library", "keychains"),
}
_SECRET_EXTENSIONS = (".pem", ".key", ".p12", ".pfx")
_ENV_TEMPLATE_SUFFIXES = (".env.example", ".env.template", ".env.sample")
_SSH_KEY_RE = re.compile(r"^([^/]*[-_.])?id_(rsa|dsa|ecdsa|ed25519)([-_.][^/]*)?$", re.IGNORECASE)


def _is_secret_name(
    name: str,
    *,
    secret_dirs=_SECRET_DIRS,
    secret_files=_SECRET_FILES,
    secret_extensions=_SECRET_EXTENSIONS,
    env_template_suffixes=_ENV_TEMPLATE_SUFFIXES,
    ssh_key_re=_SSH_KEY_RE,
) -> bool:
    """Whether a basename is credential-bearing and must be hidden from a walk."""
    lower = name.lower()
    if lower.endswith(".pub"):
        return False
    if lower in secret_dirs or lower in secret_files:
        return True
    if lower.startswith(".env"):
        return not lower.endswith(env_template_suffixes)
    if lower.endswith(secret_extensions):
        return True
    return ssh_key_re.fullmatch(lower) is not None


def _is_secret_pair(
    parent_name: str,
    child_name: str,
    *,
    path_pairs=_SECRET_PATH_PAIRS,
) -> bool:
    return (parent_name.lower(), child_name.lower()) in path_pairs


def _execute_sync(
    *, path: str = ".", max_depth: int = 2, _sensitive_rules: dict | None = None
) -> str:
    if not isinstance(path, str):
        return "Error: path must be a string"
    if isinstance(max_depth, bool) or not isinstance(max_depth, int):
        return "Error: max_depth must be an integer"
    base = os.path.realpath(path)
    if not os.path.isdir(base):
        return f"Error: not a directory: {base}"

    rules = _sensitive_rules if isinstance(_sensitive_rules, dict) else {}
    secret_dirs = rules.get("credential_dirs") or _SECRET_DIRS
    secret_files = rules.get("credential_config_files") or _SECRET_FILES
    path_pairs = rules.get("credential_path_pairs") or _SECRET_PATH_PAIRS
    secret_extensions = rules.get("private_key_extensions") or _SECRET_EXTENSIONS
    env_templates = rules.get("env_template_suffixes") or _ENV_TEMPLATE_SUFFIXES
    ssh_pattern = rules.get("ssh_key_pattern")
    ssh_re = (
        re.compile(rf"^{ssh_pattern}$", re.IGNORECASE)
        if isinstance(ssh_pattern, str)
        else _SSH_KEY_RE
    )

    def check_secret_name(name: str) -> bool:
        return _is_secret_name(
            name,
            secret_dirs=secret_dirs,
            secret_files=secret_files,
            secret_extensions=secret_extensions,
            env_template_suffixes=env_templates,
            ssh_key_re=ssh_re,
        )

    base_path = Path(base).resolve()

    def is_safe_entry(entry_path: str) -> bool:
        """Only inspect non-symlink entries that resolve below the requested base."""
        if os.path.islink(entry_path):
            return False
        try:
            return Path(entry_path).resolve().is_relative_to(base_path)
        except (OSError, ValueError):
            return False

    max_depth = max(1, min(max_depth, 5))
    lines: list[str] = [f"# {base}"]
    count = 0

    def walk(dir_path: str, prefix: str, depth: int) -> None:
        nonlocal count
        if depth > max_depth or count >= MAX_ENTRIES:
            return
        try:
            entries = sorted(os.listdir(dir_path))
        except PermissionError:
            lines.append(f"{prefix}[unreadable: permission denied]")
            return
        except OSError as e:
            lines.append(f"{prefix}[unreadable: {type(e).__name__}]")
            return

        parent_name = os.path.basename(dir_path)
        dirs = [
            entry for entry in entries
            if entry not in EXCLUDED
            and not check_secret_name(entry)
            and not _is_secret_pair(parent_name, entry, path_pairs=path_pairs)
            and is_safe_entry(os.path.join(dir_path, entry))
            and os.path.isdir(os.path.join(dir_path, entry))
        ]
        files = [
            entry for entry in entries
            if entry not in EXCLUDED
            and not check_secret_name(entry)
            and not _is_secret_pair(parent_name, entry, path_pairs=path_pairs)
            and is_safe_entry(os.path.join(dir_path, entry))
            and os.path.isfile(os.path.join(dir_path, entry))
        ]

        for f in files:
            if count >= MAX_ENTRIES:
                return
            try:
                size = os.path.getsize(os.path.join(dir_path, f))
            except OSError:
                continue
            size_str = f"{size}B" if size < 1024 else f"{size/1024:.1f}K" if size < 1048576 else f"{size/1048576:.1f}M"
            lines.append(f"{prefix}{f}  {size_str}")
            count += 1

        for d in dirs:
            if count >= MAX_ENTRIES:
                return
            lines.append(f"{prefix}{d}/")
            count += 1
            walk(os.path.join(dir_path, d), prefix + "  ", depth + 1)

    walk(base, "", 1)
    if count >= MAX_ENTRIES:
        lines.append(f"\n...truncated at {MAX_ENTRIES} entries")
    return "\n".join(lines)


async def execute(
    *, path: str = ".", max_depth: int = 2, _sensitive_rules: dict | None = None
) -> str:
    return await asyncio.to_thread(
        _execute_sync,
        path=path,
        max_depth=max_depth,
        _sensitive_rules=_sensitive_rules,
    )
