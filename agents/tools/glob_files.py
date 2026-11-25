"""Glob tool — find files by pattern."""
# 仅按路径模式枚举文件；需要查找文件内容时应使用 grep。

import asyncio
import fnmatch
import os
import re
from pathlib import Path

TOOL_META = {
    "name": "glob_files",
    "description": (
        "Find files matching a glob pattern. Returns relative paths. "
        "Examples: '**/*.py', 'src/**/*.ts', '**/test_*.py'"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'"},
            "path": {"type": "string", "description": "Base directory to search from", "default": "."},
        },
        "required": ["pattern"],
    },
    "path_args": ["path"],
    "permission_level": "read",
    "approval_policy": "never",
    "side_effect": "none",
    "execution_environment": "host",
}

MAX_RESULTS = 200
# MAX_RESULTS bounds the response; this bounds the work. Without it a pattern
# pointed at a large tree enumerated every entry before the result slice, with
# no timeout of the kind grep gets from its subprocess.
MAX_SCAN_ENTRIES = 20_000
EXCLUDED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "dist", ".build"}

# Credential-bearing names an enumeration must never surface, even by path.
# ToolGuard gates a directly-named sensitive file, but a pattern walk reaches a
# whole subtree, so this tool excludes them itself.  Kept in sync with the guard
# by engine/tests/tool/test_read_tool_secret_parity.py; `agents/` cannot import
# the engine, so the logic is mirrored rather than shared.
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


def _is_relative_pattern(pattern: str) -> bool:
    """Reject patterns that can replace or traverse out of the chosen base."""
    expanded = os.path.expanduser(pattern)
    if os.path.isabs(expanded):
        return False
    return all(part != ".." for part in expanded.replace("\\", "/").split("/"))


def _is_within_base(path: str | Path, base: str | Path) -> bool:
    """Resolve symlinks before deciding whether a match belongs to ``base``."""
    try:
        return Path(path).resolve().is_relative_to(Path(base).resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def _matches_component(name: str, pattern: str) -> bool:
    """Match one path component with the same hidden-file rule as ``glob``."""
    return not (name.startswith(".") and not pattern.startswith(".")) and fnmatch.fnmatchcase(name, pattern)


def _safe_glob(
    base: Path,
    pattern: str,
    *,
    sensitive_rules: dict | None = None,
) -> tuple[list[Path], bool]:
    """Expand a relative glob without following symlinked directories."""
    rules = sensitive_rules if isinstance(sensitive_rules, dict) else {}
    secret_dirs = rules.get("credential_dirs") or _SECRET_DIRS
    secret_files = rules.get("credential_config_files") or _SECRET_FILES
    path_pairs = rules.get("credential_path_pairs") or _SECRET_PATH_PAIRS
    secret_extensions = rules.get("private_key_extensions") or _SECRET_EXTENSIONS
    env_templates = rules.get("env_template_suffixes") or _ENV_TEMPLATE_SUFFIXES
    ssh_pattern = rules.get("ssh_key_pattern")
    ssh_re = re.compile(rf"^{ssh_pattern}$", re.IGNORECASE) if isinstance(ssh_pattern, str) else _SSH_KEY_RE

    def check_secret_name(name: str) -> bool:
        return _is_secret_name(
            name,
            secret_dirs=secret_dirs,
            secret_files=secret_files,
            secret_extensions=secret_extensions,
            env_template_suffixes=env_templates,
            ssh_key_re=ssh_re,
        )

    components = [part for part in pattern.replace("\\", "/").split("/") if part not in {"", "."}]
    collapsed: list[str] = []
    for part in components:
        if part == "**" and collapsed and collapsed[-1] == "**":
            continue
        collapsed.append(part)
    components = collapsed
    matches: list[Path] = []
    visited: set[tuple[Path, int]] = set()
    scanned = 0
    truncated = False

    def entries(directory: Path):
        nonlocal scanned, truncated
        if truncated:
            return ()
        try:
            if directory.is_symlink() or not _is_within_base(directory, base):
                return ()
            with os.scandir(directory) as scan:
                items = tuple(sorted(scan, key=lambda entry: entry.name))
        except OSError:
            return ()
        scanned += len(items)
        if scanned > MAX_SCAN_ENTRIES:
            truncated = True
        return items

    def add_file(candidate: Path) -> None:
        try:
            if (
                not candidate.is_symlink()
                and not check_secret_name(candidate.name)
                and not _is_secret_pair(candidate.parent.name, candidate.name, path_pairs=path_pairs)
                and candidate.is_file()
                and _is_within_base(candidate, base)
            ):
                matches.append(candidate)
        except OSError:
            return

    stack: list[tuple[Path, int]] = [(base, 0)]
    while stack:
        directory, index = stack.pop()
        key = (directory, index)
        if key in visited:
            continue
        visited.add(key)
        if index == len(components):
            add_file(directory)
            continue

        component = components[index]
        if component == "**":
            stack.append((directory, index + 1))
            for entry in entries(directory):
                if (
                    entry.name in EXCLUDED_DIRS
                    or entry.name.startswith(".")
                    or check_secret_name(entry.name)
                    or _is_secret_pair(directory.name, entry.name, path_pairs=path_pairs)
                    or entry.is_symlink()
                ):
                    continue
                candidate = Path(entry.path)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((candidate, index))
                    elif index == len(components) - 1 and entry.is_file(follow_symlinks=False):
                        add_file(candidate)
                except OSError:
                    continue
            continue

        for entry in entries(directory):
            if (
                entry.name in EXCLUDED_DIRS
                or check_secret_name(entry.name)
                or _is_secret_pair(directory.name, entry.name, path_pairs=path_pairs)
                or entry.is_symlink()
                or not _matches_component(entry.name, component)
            ):
                continue
            candidate = Path(entry.path)
            try:
                if index == len(components) - 1:
                    if entry.is_file(follow_symlinks=False):
                        add_file(candidate)
                elif entry.is_dir(follow_symlinks=False):
                    stack.append((candidate, index + 1))
            except OSError:
                continue

    return matches, truncated


def _execute_sync(
    *, pattern: str, path: str = ".", _sensitive_rules: dict | None = None
) -> str:
    if not _is_relative_pattern(pattern):
        return "Error: glob pattern must be relative to the requested directory"

    base = os.path.realpath(path)
    if not os.path.isdir(base):
        return f"Error: directory not found: {base}"

    matches, scan_truncated = _safe_glob(
        Path(base), pattern, sensitive_rules=_sensitive_rules
    )
    filtered = [
        os.path.relpath(m, base) for m in matches
        if not any(part in EXCLUDED_DIRS for part in m.relative_to(base).parts)
    ]

    filtered = sorted(filtered)
    total = len(filtered)
    filtered = filtered[:MAX_RESULTS]
    if not filtered:
        if scan_truncated:
            return (
                f"Error: scan stopped after {MAX_SCAN_ENTRIES} directory entries "
                f"without matching '{pattern}'; narrow the base directory or pattern"
            )
        return f"No files found matching: {pattern}"

    header = f"# {min(total, MAX_RESULTS)} file{'s' if len(filtered) != 1 else ''}"
    if total > MAX_RESULTS:
        header += f" (showing {MAX_RESULTS} of {total})"
    if scan_truncated:
        header += f" — scan stopped after {MAX_SCAN_ENTRIES} entries, results may be incomplete"
    return header + "\n" + "\n".join(filtered)


async def execute(
    *, pattern: str, path: str = ".", _sensitive_rules: dict | None = None
) -> str:
    return await asyncio.to_thread(
        _execute_sync,
        pattern=pattern,
        path=path,
        _sensitive_rules=_sensitive_rules,
    )
