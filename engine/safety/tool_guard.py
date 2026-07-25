"""Tool safety guard — permission levels, path checking, audit logging."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from engine.tool.interface import ToolCall, ToolDefinition


# ── Permission Levels (req #2: default-deny + tiered approval) ──

class PermissionLevel(Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    DESTRUCTIVE = "destructive"


@dataclass
class GuardResult:
    allowed: bool
    reason: str = ""
    level: PermissionLevel = PermissionLevel.READ
    needs_confirmation: bool = False
    # True when the call passed hard safety checks but must wait for a user
    # approval before the provider is invoked.
    approval_required: bool = False
    # True when the only problem is that the path sits outside the allowed
    # directories — the one block a session whitelist may override.  Sensitive
    # blocks (.ssh, .env writes, .git, …) keep this False and are never
    # bypassable.
    boundary_block: bool = False


# ── Path guard (req #3: symlink, traversal, sensitive files) ──

class FileGuard:
    _ALWAYS_BLOCKED = frozenset({".ssh", ".gnupg", ".aws", ".kube"})
    _SENSITIVE_WRITE = frozenset({".env", ".env.local", ".env.production", ".npmrc", ".pypirc"})
    _SENSITIVE_DIRS = frozenset({".git"})

    def __init__(self, allowed_dirs: list[Path] | None = None):
        self._working_dir: Path | None = None
        self.set_allowed_dirs(allowed_dirs)

    def set_allowed_dirs(self, allowed_dirs: list[Path] | None) -> None:
        if allowed_dirs:
            self._allowed = [p.resolve() for p in allowed_dirs]
        else:
            self._allowed = [Path.home().resolve(), Path("/tmp").resolve(), Path.cwd().resolve()]

    def set_working_directory(self, working_dir: Path) -> None:
        root = Path(working_dir).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"working directory does not exist: {working_dir}")
        self._working_dir = root
        self.set_allowed_dirs([root])

    @property
    def is_working_directory_scoped(self) -> bool:
        return self._working_dir is not None

    def check_path(self, path_str: str, writing: bool = False) -> GuardResult:
        try:
            # Shell expands a leading tilde before execution; mirror that here
            # so the guard evaluates the same target the command will touch.
            candidate = Path(path_str).expanduser()
            if not candidate.is_absolute() and self._working_dir is not None:
                candidate = self._working_dir / candidate
            lexical = Path(os.path.abspath(str(candidate)))
            target = candidate.resolve()
        except (ValueError, OSError):
            return GuardResult(allowed=False, reason=f"Invalid path: {path_str}")

        for part in target.parts:
            if part in self._ALWAYS_BLOCKED:
                return GuardResult(allowed=False, reason=f"Access to {part}/ is blocked")

        if writing:
            name = target.name.lower()
            if name in self._SENSITIVE_WRITE or name.startswith(".env"):
                return GuardResult(allowed=False, reason=f"Write to sensitive file blocked: {name}", needs_confirmation=True)
            for part in target.parts:
                if part in self._SENSITIVE_DIRS:
                    return GuardResult(allowed=False, reason=f"Write inside {part}/ is blocked", needs_confirmation=True)

            # The runtime owns ~/.agent-smith. Only the three memory views are
            # writable through model-facing file tools; lifecycle code writes
            # its internal checkpoints through the trusted memory service.
            try:
                lexical_platform = lexical.is_relative_to(_PLATFORM_DATA_ROOT)
                resolved_platform = target.is_relative_to(_PLATFORM_DATA_ROOT)
                lexical_memory = (
                    lexical.is_relative_to(_MEMORY_WRITE_ROOT)
                    and lexical.name in _MEMORY_WRITE_FILES
                )
                resolved_memory = (
                    target.is_relative_to(_MEMORY_WRITE_ROOT)
                    and target.name in _MEMORY_WRITE_FILES
                )
            except ValueError:
                lexical_platform = resolved_platform = True
                lexical_memory = resolved_memory = False

            if (lexical_platform or resolved_platform) and not (
                lexical_memory and resolved_memory
            ):
                return GuardResult(
                    allowed=False,
                    reason=(
                        "[platform-protect-001] Platform runtime writes are restricted; "
                        "only controlled memory files may be written."
                    ),
                )

        if any(target.is_relative_to(d) for d in self._allowed):
            return GuardResult(allowed=True)

        return GuardResult(
            allowed=False,
            reason=f"Path {path_str} outside allowed directories",
            boundary_block=True,
        )


# ── Audit log (req #6: every tool call logged) ──────────────

class AuditLog:
    def __init__(self, log_path: Optional[Path] = None):
        if log_path is None:
            try:
                from common.config import DATA_DIR
                log_path = DATA_DIR / "audit.jsonl"
            except Exception:
                log_path = Path.home() / ".agent-smith" / "audit.jsonl"
        self._path = log_path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, tool_name: str, arguments: dict, result: GuardResult, **extra: object) -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "tool": tool_name,
            "args_summary": _summarize_args(arguments),
            "allowed": result.allowed,
            "level": result.level.value,
            "reason": result.reason or None,
            **_summarize_args(extra),
        }
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass


_SENSITIVE_ARG_KEY_PARTS = (
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "passwd",
    "password",
    "privatekey",
    "secret",
    "token",
)


def _is_sensitive_arg_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(part in normalized for part in _SENSITIVE_ARG_KEY_PARTS)


def _summarize_value(value: object, max_len: int) -> object:
    if isinstance(value, dict):
        return _summarize_args(value, max_len=max_len)
    if isinstance(value, list):
        return [_summarize_value(item, max_len) for item in value]
    if isinstance(value, tuple):
        return tuple(_summarize_value(item, max_len) for item in value)
    if isinstance(value, str) and len(value) > max_len:
        return value[:max_len] + f"...({len(value)} chars)"
    return value


def _summarize_args(args: dict, max_len: int = 200) -> dict:
    """Recursively redact credential-bearing argument fields for audit storage."""
    redacted = {}
    for k, v in args.items():
        if _is_sensitive_arg_key(k):
            redacted[k] = "***"
        else:
            redacted[k] = _summarize_value(v, max_len)
    return redacted


# ── Session whitelist (req #5: session-scoped overrides) ────

class SessionWhitelist:
    def __init__(self) -> None:
        self._allowed_tools: set[str] = set()
        self._allowed_paths: set[str] = set()
        self._allowed_files: set[str] = set()

    def allow_tool(self, tool_name: str) -> None:
        self._allowed_tools.add(tool_name)

    def allow_path(self, path: str) -> None:
        self._allowed_paths.add(str(Path(path).resolve()))

    def allow_file(self, path: str) -> None:
        self._allowed_files.add(str(Path(path).resolve()))

    def is_tool_allowed(self, tool_name: str) -> bool:
        return tool_name in self._allowed_tools

    def is_path_allowed(self, path: str) -> bool:
        try:
            resolved = Path(path).resolve()
        except (ValueError, OSError):
            return False
        if str(resolved) in self._allowed_files:
            return True
        for p in self._allowed_paths:
            base = Path(p)
            try:
                if resolved == base or resolved.is_relative_to(base):
                    return True
            except ValueError:
                continue
        return False

    def clear(self) -> None:
        self._allowed_tools.clear()
        self._allowed_paths.clear()
        self._allowed_files.clear()


# ── Shell path extraction (req #4: redirects + pipes) ───────

_REDIRECT_RE = re.compile(r"(?:>>?|[12]>>?|&>>?)\s*([^\s;|&]+)")
_ABS_PATH_RE = re.compile(r"(?<![\w:~])/(?!/)[^\s;|&'\"`$()<>]+")
_SHELL_CD_RE = re.compile(
    r"(?:^|[;|&]\s*)cd\s+(?:--\s+)?(?P<path>'[^']*'|\"[^\"]*\"|[^\s;|&]+)"
)

_PLATFORM_DATA_ROOT = (Path.home() / ".agent-smith").resolve()
_MEMORY_WRITE_ROOT = _PLATFORM_DATA_ROOT / "agent" / "memory"
_MEMORY_WRITE_FILES = frozenset({"recent.jsonl", "recent.md", "durable.md"})


def _extract_shell_paths(command: str) -> tuple[list[str], list[str]]:
    read_paths = _ABS_PATH_RE.findall(command)
    write_paths = [m.strip("'\"") for m in _REDIRECT_RE.findall(command)]
    for match in _SHELL_CD_RE.finditer(command):
        path = match.group("path").strip("'\"")
        if path:
            read_paths.append(path)
    return read_paths, write_paths


def _rule_match_targets(arguments: dict) -> list[str]:
    """Strings that dangerous-command rule patterns are matched against.

    The JSON dump alone breaks ``$``-anchored patterns (every value in the
    dump is followed by ``"``), so each raw string value is matched as well.
    """
    targets: list[str] = [json.dumps(arguments, ensure_ascii=False)]
    stack: list[object] = list(arguments.values())
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        elif isinstance(current, str):
            targets.append(current)
    return targets


# ── Main guard ──────────────────────────────────────────────

class ToolGuard:
    def __init__(
        self,
        rules_path: Path,
        allowed_dirs: list[Path] | None = None,
        *,
        tool_registry: dict[str, ToolDefinition] | None = None,
    ) -> None:
        self._rules: list[dict] = []
        if rules_path.is_file():
            self._rules = json.loads(rules_path.read_text(encoding="utf-8"))
        self.file_guard = FileGuard(allowed_dirs)
        self.audit = AuditLog()
        self.whitelist = SessionWhitelist()
        self._tool_registry: dict[str, ToolDefinition] = tool_registry or {}

    def bind_definitions(self, definitions: dict[str, ToolDefinition]) -> None:
        """Bind tool definitions after registry load so metadata-first checks apply."""
        self._tool_registry = definitions

    def set_allowed_dirs(self, allowed_dirs: list[Path]) -> None:
        """Replace the file boundary for the current request's project root."""
        self.file_guard.set_allowed_dirs(allowed_dirs)

    def allow_project_instruction_path(self, project_root: Path) -> Path:
        """Whitelist only the canonical project instruction file for an explicit /init action."""
        target = project_root.resolve() / ".smith" / "SMITH.md"
        self.whitelist.allow_file(str(target))
        return target

    def set_working_directory(self, working_dir: Path) -> None:
        """Constrain project-facing file tools to one resolved workspace."""
        self.file_guard.set_working_directory(working_dir)

    def _resolve_path_metadata(
        self,
        tool_name: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...], bool, bool]:
        """Return path and opaque-command metadata for a registered tool."""
        defn = self._tool_registry.get(tool_name)
        if defn is None:
            return (), (), False, False
        return defn.path_args, defn.list_path_args, defn.is_write_tool, defn.opaque_command

    def _path_arg_uses_nonempty_default(self, tool_name: str, arg_name: str) -> bool:
        """Whether an omitted path would make the provider use an implicit path."""
        definition = self._tool_registry.get(tool_name)
        if definition is not None and isinstance(definition.parameters, dict):
            properties = definition.parameters.get("properties")
            parameter = properties.get(arg_name) if isinstance(properties, dict) else None
            default = parameter.get("default") if isinstance(parameter, dict) else None
            return isinstance(default, str) and bool(default)
        return False

    def _check_file_paths(self, tool_call: ToolCall) -> GuardResult | None:
        path_args, list_path_args, is_write, opaque_command = self._resolve_path_metadata(tool_call.name)
        if not path_args and not list_path_args and not opaque_command:
            return None

        paths_to_check: list[tuple[str, bool]] = []

        for arg_name in path_args:
            path_val = tool_call.arguments.get(arg_name)
            if path_val:
                paths_to_check.append((str(path_val), is_write))
            elif (
                self.file_guard.is_working_directory_scoped
                and self._path_arg_uses_nonempty_default(tool_call.name, arg_name)
            ):
                # The registry must first materialize the provider's default
                # (such as ``path='.'``) within the workspace.  Letting a raw
                # optional path through here would make the provider use the
                # server process CWD instead.
                return GuardResult(
                    allowed=False,
                    reason=(
                        f"Path argument '{arg_name}' must be canonicalized "
                        "before policy checks"
                    ),
                    boundary_block=True,
                )

        cwd_val = tool_call.arguments.get("cwd")
        cwd = str(cwd_val) if cwd_val else ""

        for arg_name in list_path_args:
            raw_values = tool_call.arguments.get(arg_name) or []
            if not isinstance(raw_values, list):
                continue
            for raw in raw_values:
                p = Path(str(raw))
                if cwd and not p.is_absolute():
                    p = Path(cwd) / p
                paths_to_check.append((str(p), is_write))

        if opaque_command:
            cmd = tool_call.arguments.get("command", "")
            if self.file_guard.is_working_directory_scoped:
                return GuardResult(
                    allowed=False,
                    reason="Shell is unavailable while a working-directory boundary is active",
                )
            _, write_paths = _extract_shell_paths(cmd)
            for wp in write_paths:
                # A raw shell command cannot be safely parsed into a complete
                # filesystem access list.  It is therefore always approved by
                # the user (see the shell metadata), rather than pretending a
                # partial regex enforces the project boundary.  Preserve the
                # non-bypassable platform-runtime write protection for literal
                # paths, which is independent of the project boundary.
                try:
                    candidate = Path(wp).expanduser().resolve()
                except (ValueError, OSError):
                    continue
                if candidate.is_relative_to(_PLATFORM_DATA_ROOT):
                    paths_to_check.append((wp, True))

        for p, writing in paths_to_check:
            result = self.file_guard.check_path(p, writing=writing)
            if not result.allowed:
                # The session whitelist may extend the directory boundary, but
                # it never bypasses sensitive-path blocks (.ssh, .env writes,
                # .git, …) — those return boundary_block=False.
                if result.boundary_block and self.whitelist.is_path_allowed(p):
                    continue
                return result
        return None

    def _resolve_permission_level(self, tool_name: str) -> PermissionLevel:
        """Return the permission level declared by the registered tool.

        Unknown or unregistered tools are treated as host execution. This is
        intentionally conservative and avoids a second, name-based security
        registry drifting away from the provider metadata.
        """
        defn = self._tool_registry.get(tool_name)
        if defn is not None and defn.permission_level:
            try:
                return PermissionLevel(defn.permission_level)
            except ValueError:
                pass
        return PermissionLevel.EXECUTE

    def _resolve_approval_policy(self, tool_name: str) -> str:
        """Resolve approval requirements from registered tool metadata."""
        defn = self._tool_registry.get(tool_name)
        if defn is None:
            return "always"
        if defn.approval_policy != "never":
            return defn.approval_policy
        if (
            defn.is_write_tool
            or defn.side_effect != "none"
            or defn.permission_level in {"write", "execute", "destructive"}
        ):
            return "policy"
        return "never"

    def _requires_approval(self, tool_call: ToolCall) -> bool:
        policy = self._resolve_approval_policy(tool_call.name)
        if policy == "never":
            return False
        if policy == "always":
            return True
        definition = self._tool_registry.get(tool_call.name)
        action = str(tool_call.arguments.get("action") or "").lower()
        if definition is not None and action in definition.read_actions:
            return False
        return True

    def check(self, tool_call: ToolCall) -> GuardResult:
        level = self._resolve_permission_level(tool_call.name)
        tool_whitelisted = self.whitelist.is_tool_allowed(tool_call.name)

        file_result = self._check_file_paths(tool_call)
        if file_result is not None:
            file_result.level = level
            self.audit.record(tool_call.name, tool_call.arguments, file_result)
            return file_result

        match_targets = _rule_match_targets(tool_call.arguments)
        for rule in self._rules:
            scoped_tools = rule.get("tools")
            if scoped_tools and tool_call.name not in scoped_tools:
                continue

            patterns = rule.get("patterns", [])
            single = rule.get("pattern", "")
            if single:
                patterns = [single] + list(patterns)

            exclude_patterns = rule.get("excludePatterns", [])

            for pattern in patterns:
                if not pattern:
                    continue
                for target in match_targets:
                    if not re.search(pattern, target):
                        continue
                    if any(ep and re.search(ep, target) for ep in exclude_patterns):
                        continue
                    reason = rule.get("reason") or rule.get("description") or f"Blocked: {pattern}"
                    result = GuardResult(
                        allowed=False,
                        reason=f"[{rule.get('id', '?')}] {reason}",
                        level=PermissionLevel.DESTRUCTIVE,
                    )
                    self.audit.record(tool_call.name, tool_call.arguments, result, rule_id=rule.get("id"))
                    return result

        approval_required = self._requires_approval(tool_call)
        result = GuardResult(
            allowed=True,
            level=level,
            reason=f"Approval required for {tool_call.name}" if approval_required else "",
            approval_required=approval_required,
        )
        self.audit.record(
            tool_call.name,
            tool_call.arguments,
            result,
            whitelisted=tool_whitelisted,
        )
        return result
