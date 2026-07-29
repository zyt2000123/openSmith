"""Tool safety guard — permission levels, path checking, audit logging."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from engine.safety.approval import ApprovalScope, current_approval_context
from engine.tool.interface import ToolCall, ToolDefinition

logger = logging.getLogger(__name__)


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
    # True when the only issue is the active working-directory boundary. A
    # session-level trusted whitelist may extend that boundary; high-risk
    # approvals remain visible even when a path is otherwise reachable.
    boundary_block: bool = False
    # The user-visible, one-shot capability requested for this call. It is
    # carried through the approval broker and re-checked by ToolRegistry.
    approval_scope: ApprovalScope | None = None


# ── Path guard (req #3: symlink, traversal, sensitive files) ──

def _casefolded(path: Path) -> Path:
    """Lowercase a path, for security comparisons only — never for I/O.

    macOS (APFS) and Windows are case-insensitive but case-preserving, so
    ``.GIT/hooks/pre-commit`` reaches the very same inode as ``.git/...`` while
    ``Path.resolve()`` faithfully preserves the caller's casing.  Any guard that
    compares path components therefore has to fold case first, or the block is
    bypassable by simply retyping the path.  On case-sensitive filesystems this
    can only ever over-block, never under-block.
    """
    return Path(str(path).lower())


_HARDLINK_SCAN_SKIP = frozenset({"objects", "node_modules", ".venv", "__pycache__"})
# Files git always keeps in a real git directory (plain repo, worktree gitdir, or
# submodule gitdir all carry HEAD).
_GIT_DIR_MARKERS = ("HEAD", "config", "commondir")


def _looks_like_git_dir(path: Path) -> bool:
    """True when ``path`` is plausibly a git directory rather than any directory."""
    try:
        if not path.is_dir():
            return False
        return any((path / marker).exists() for marker in _GIT_DIR_MARKERS)
    except OSError:
        return False


def _git_dirs_for(working_dir: Path) -> list[Path]:
    """Resolve ``.git`` whether it is a directory or a worktree/submodule pointer.

    In a ``git worktree`` (and in submodules) ``.git`` is a *file* containing
    ``gitdir: <path>``, and the hooks that matter live in the main repository —
    so treating a non-directory ``.git`` as "nothing to scan" left the hard-link
    check inert for exactly the layout this repository itself uses.
    """
    git_path = working_dir / ".git"
    if git_path.is_dir():
        return [git_path]
    if not git_path.is_file():
        return []
    try:
        text = git_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return []
    if not text.startswith("gitdir:"):
        return []
    pointer = Path(text[len("gitdir:"):].strip()).expanduser()
    if not pointer.is_absolute():
        pointer = working_dir / pointer
    try:
        pointer = pointer.resolve()
    except OSError:
        return []
    candidates = [pointer]
    # A worktree's gitdir is <main>/.git/worktrees/<id>; hooks live in the common
    # dir two levels up.  (A submodule's is <main>/.git/modules/<name>, which
    # carries its own hooks/ and must not be lifted.)
    if pointer.parent.name == "worktrees":
        candidates.append(pointer.parent.parent)
    # Only follow a pointer that actually looks like a git directory.  This file
    # is not covered by any write guard — it arrives with a cloned repo — so an
    # unvalidated pointer would let untrusted content aim the walk below at any
    # path, turning every nlink>1 write check into a scan of, say, /usr.
    return [path for path in candidates if _looks_like_git_dir(path)]


def _shares_inode_with_protected_file(target: Path, working_dir: Path | None) -> bool:
    """True when ``target`` is a hard link into a protected location.

    A hard link gives one inode a second name, so a harmless-looking path can
    write straight through to ``.git/hooks/pre-commit``; name-based checks cannot
    see that.  Rejecting *every* multiply-linked file would be simpler but breaks
    trees that use hard links normally — pnpm's content-addressed store, ccache,
    ``cp -al`` backups — so confirm the inode really is reachable from a
    protected directory before refusing.

    Short-circuits on ``st_nlink == 1``, which covers virtually every write, so
    the walk below never runs on the hot path.  ``.git/objects`` is skipped:
    those blobs are content-addressed, immutable and checksum-verified by git.
    """
    try:
        info = os.lstat(target)
    except OSError:
        return False
    if info.st_nlink < 2 or not os.path.isfile(target):
        return False

    identity = (info.st_dev, info.st_ino)
    home = Path.home()
    roots = [home / name for name in FileGuard._ALWAYS_BLOCKED]
    roots.append(_PLATFORM_DATA_ROOT)
    if working_dir is not None:
        roots.extend(_git_dirs_for(working_dir))
        roots.extend(working_dir / name for name in FileGuard._ALWAYS_BLOCKED)

    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in _HARDLINK_SCAN_SKIP]
            for filename in filenames:
                try:
                    candidate = os.lstat(os.path.join(dirpath, filename))
                except OSError:
                    continue
                if (candidate.st_dev, candidate.st_ino) == identity:
                    return True
    return False


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

    @staticmethod
    def _path_approval(
        target: Path,
        *,
        writing: bool,
        reason: str,
        high_risk: bool = False,
        boundary_block: bool = False,
    ) -> GuardResult:
        return GuardResult(
            allowed=False,
            reason=reason,
            level=PermissionLevel.WRITE if writing else PermissionLevel.READ,
            needs_confirmation=True,
            approval_required=True,
            boundary_block=boundary_block,
            approval_scope=ApprovalScope.path(
                str(target),
                writing=writing,
                high_risk=high_risk,
            ),
        )

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

        if _is_runtime_credential_path(target, lexical):
            return GuardResult(
                allowed=False,
                reason=(
                    "[runtime-secret-001] Agent runtime credential files are not "
                    "delegable to model tools"
                ),
            )
        if _shares_inode_with_runtime_credential(target):
            return GuardResult(
                allowed=False,
                reason=(
                    "[runtime-secret-001] Agent runtime credential files are not "
                    "delegable through a hard-link alias"
                ),
            )

        # Check the literal path as well as the resolved one: a credential
        # directory that is itself a symlink resolves to a name carrying no
        # ``.ssh``/``.aws`` component and would otherwise slip through.  The
        # sensitive-write branch below already pairs lexical with resolved.
        for part in (*target.parts, *lexical.parts):
            if part.lower() in self._ALWAYS_BLOCKED:
                return self._path_approval(
                    target,
                    writing=writing,
                    high_risk=True,
                    reason=(
                        f"Access to {part}/ contains user credentials and requires "
                        "high-risk approval"
                    ),
                )

        if writing:
            name = target.name.lower()
            if name in self._SENSITIVE_WRITE or name.startswith(".env"):
                return self._path_approval(
                    target,
                    writing=True,
                    high_risk=True,
                    reason=f"Write to sensitive file {name} requires high-risk approval",
                )
            for part in target.parts:
                if part.lower() in self._SENSITIVE_DIRS:
                    return self._path_approval(
                        target,
                        writing=True,
                        high_risk=True,
                        reason=f"Write inside {part}/ requires high-risk approval",
                    )

            # Runtime-managed state is high-risk but, unlike the provider
            # credential files above, belongs to the local user and may be
            # touched after a visible, exact approval. The controlled memory
            # files retain their ordinary tool policy so lifecycle maintenance
            # does not create a second special path.
            try:
                # Case-fold the *block* test so a retyped ``.Agent-Smith`` cannot
                # slip past it; the memory allow-list below stays case-exact so
                # folding never widens the write exemption.
                folded_root = _casefolded(_PLATFORM_DATA_ROOT)
                lexical_platform = _casefolded(lexical).is_relative_to(folded_root)
                resolved_platform = _casefolded(target).is_relative_to(folded_root)
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
                return self._path_approval(
                    target,
                    writing=True,
                    high_risk=True,
                    reason=(
                        "[platform-state-001] Writing Agent-Smith runtime state "
                        "requires high-risk approval"
                    ),
                )

            # A hard link is a second name for one inode, so a path mentioning
            # nothing sensitive can still write straight into .git/hooks/ or
            # .ssh/.  Nothing above catches it: every check so far reasons about
            # path *names*, never inode identity.
            if _shares_inode_with_protected_file(target, self._working_dir):
                return GuardResult(
                    allowed=False,
                    reason=(
                        f"[unsafe-alias-001] Cannot safely represent the write to "
                        f"{target.name}: it is a hard link to a protected file."
                    ),
                )

        if any(target.is_relative_to(d) for d in self._allowed):
            return GuardResult(allowed=True)

        if self.is_working_directory_scoped:
            # A project-scoped run may inspect or change a user-selected
            # external path only after the live approval flow has bound that
            # exact normalized call.
            return self._path_approval(
                target,
                writing=writing,
                reason=(
                    f"Path {path_str} is outside the active working directory "
                    "and requires explicit user approval"
                ),
                boundary_block=True,
            )

        return self._path_approval(
            target,
            writing=writing,
            reason=f"Path {path_str} is outside the active directories and requires approval",
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
            logger.warning("failed to append tool safety audit", exc_info=True)


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

_PLATFORM_DATA_ROOT = (Path.home() / ".agent-smith").resolve()
_MEMORY_WRITE_ROOT = _PLATFORM_DATA_ROOT / "agent" / "memory"
_MEMORY_WRITE_FILES = frozenset({"recent.jsonl", "recent.md", "durable.md"})
_RUNTIME_CREDENTIAL_PATHS = frozenset({
    _PLATFORM_DATA_ROOT / "config.yaml",
    _PLATFORM_DATA_ROOT / "config.yml",
    _PLATFORM_DATA_ROOT / "agent" / "config.yaml",
    _PLATFORM_DATA_ROOT / "agent" / "config.yml",
})


def _is_runtime_credential_path(*paths: Path) -> bool:
    """Whether a model-facing path would expose a provider/runtime credential."""
    folded_secrets = {_casefolded(path) for path in _RUNTIME_CREDENTIAL_PATHS}
    return any(_casefolded(path) in folded_secrets for path in paths)


def _shares_inode_with_runtime_credential(target: Path) -> bool:
    """Reject a hard-link alias of an Agent runtime credential file.

    ``Path.resolve`` catches symlinks but intentionally cannot distinguish a
    second name for the same inode. The small, explicit credential set makes
    this comparison cheap enough for every model-facing file read and write.
    """
    try:
        target_info = os.lstat(target)
    except OSError:
        return False
    if target_info.st_nlink < 2 or not os.path.isfile(target):
        return False
    identity = (target_info.st_dev, target_info.st_ino)
    for credential_path in _RUNTIME_CREDENTIAL_PATHS:
        try:
            credential_info = os.lstat(credential_path)
        except OSError:
            continue
        if (credential_info.st_dev, credential_info.st_ino) == identity:
            return True
    return False


def _command_mentions_runtime_credential(command: str) -> bool:
    """Catch the literal shell spellings of non-delegable runtime config files.

    Shell is intentionally opaque, so this helper is only an early diagnostic;
    the Seatbelt profile independently denies the resolved runtime-secret paths
    for variable expansion and other forms this text check cannot parse.
    """
    home = str(Path.home())
    aliases: set[str] = set()
    for path in _RUNTIME_CREDENTIAL_PATHS:
        rendered = str(path)
        aliases.add(rendered)
        if rendered.startswith(home):
            suffix = rendered[len(home):]
            aliases.add(f"~{suffix}")
            aliases.add(f"$HOME{suffix}")
            aliases.add(f"${{HOME}}{suffix}")
    return any(alias in command for alias in aliases)


def _extract_shell_write_paths(command: str) -> list[str]:
    """Extract literal redirect targets from a raw shell command.

    Only writes are extracted.  A shell command's read set cannot be derived
    from a regex, so the boundary for reads is the user approval that every
    shell call already requires — not a partial pattern match.
    """
    return [m.strip("'\"") for m in _REDIRECT_RE.findall(command)]


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
            # Validate the shape here rather than letting every later check()
            # raise: a mis-edited rules file must fail loudly at load time, not
            # turn each agent run into an opaque failure.
            parsed = json.loads(rules_path.read_text(encoding="utf-8"))
            if not isinstance(parsed, list) or not all(
                isinstance(rule, dict) for rule in parsed
            ):
                raise ValueError(
                    f"dangerous-command rules must be a list of objects: {rules_path}"
                )
            self._rules = parsed
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
            definition = self._tool_registry.get(tool_call.name)
            requires_sandbox = (
                definition is not None
                and definition.execution_environment == "sandbox"
            )
            if self.file_guard.is_working_directory_scoped and not requires_sandbox:
                return GuardResult(
                    allowed=False,
                    reason=(
                        "Opaque command execution requires a sandbox while a "
                        "working-directory boundary is active"
                    ),
                )
            write_paths = _extract_shell_write_paths(cmd)
            for wp in write_paths:
                # A raw shell command cannot be safely parsed into a complete
                # filesystem access list.  It is therefore always approved by
                # the user (see the shell metadata), rather than pretending a
                # partial regex enforces the project boundary. Preserve a
                # high-risk approval for literal runtime-state writes; provider
                # credential files remain non-delegable in FileGuard itself.
                try:
                    candidate = Path(wp).expanduser().resolve()
                except (ValueError, OSError):
                    continue
                if candidate.is_relative_to(_PLATFORM_DATA_ROOT):
                    paths_to_check.append((wp, True))

        for p, writing in paths_to_check:
            result = self.file_guard.check_path(p, writing=writing)
            if not result.allowed:
                # The session whitelist may extend only the ordinary directory
                # boundary; high-risk paths do not set ``boundary_block``.
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
        definition = self._tool_registry.get(tool_call.name)
        if definition is not None and getattr(definition, "network_access", False):
            return True
        policy = self._resolve_approval_policy(tool_call.name)
        if policy == "never":
            return False
        if policy == "always":
            return True
        action = str(tool_call.arguments.get("action") or "").lower()
        if definition is not None and action in definition.read_actions:
            return False
        return True

    def _approval_scope_for(
        self,
        tool_call: ToolCall,
        *,
        high_risk: bool = False,
    ) -> ApprovalScope:
        """Describe the smallest capability that can execute this call.

        This is deliberately derived from provider metadata rather than a
        second table of tool names. Opaque shell text is not safely parseable,
        so its one approved command receives an explicit host-command scope.
        """
        path_args, list_path_args, is_write, opaque_command = self._resolve_path_metadata(
            tool_call.name
        )
        if opaque_command:
            return ApprovalScope.host_command(
                str(tool_call.arguments.get("command") or ""),
                high_risk=high_risk,
            )

        definition = self._tool_registry.get(tool_call.name)
        if definition is not None and getattr(definition, "network_access", False):
            target = (
                tool_call.arguments.get("url")
                or tool_call.arguments.get("query")
                or tool_call.arguments.get("target")
                or tool_call.name
            )
            return ApprovalScope.network(str(target), high_risk=high_risk)

        for argument_name in path_args:
            value = tool_call.arguments.get(argument_name)
            if value:
                return ApprovalScope.path(
                    str(value),
                    writing=is_write,
                    high_risk=high_risk,
                )
        for argument_name in list_path_args:
            values = tool_call.arguments.get(argument_name)
            if isinstance(values, list) and values:
                return ApprovalScope.path(
                    str(values[0]),
                    writing=is_write,
                    high_risk=high_risk,
                )
        return ApprovalScope.operation(tool_call.name, high_risk=high_risk)

    def check(self, tool_call: ToolCall, *, audit: bool = True) -> GuardResult:
        """Evaluate one call.

        ``audit=False`` suppresses the audit record for a re-check of a call
        that was already evaluated and logged.  The registry runs this guard a
        second time as an execution backstop; without this the audit trail
        would report twice the number of tool calls that actually happened.
        """
        level = self._resolve_permission_level(tool_call.name)
        tool_whitelisted = self.whitelist.is_tool_allowed(tool_call.name)
        approval_context = current_approval_context()
        audit_context: dict[str, object] = {"call_id": tool_call.id}
        if approval_context is not None:
            audit_context["run_id"] = approval_context[1]

        def record(result: GuardResult, **extra: object) -> None:
            if audit:
                self.audit.record(
                    tool_call.name,
                    tool_call.arguments,
                    result,
                    **extra,
                    **audit_context,
                )

        if self._resolve_path_metadata(tool_call.name)[3] and _command_mentions_runtime_credential(
            str(tool_call.arguments.get("command") or "")
        ):
            result = GuardResult(
                allowed=False,
                reason=(
                    "[runtime-secret-001] Agent runtime credential files are not "
                    "delegable to model tools"
                ),
                level=level,
            )
            record(result)
            return result

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
                        reason=(
                            f"[{rule.get('id', '?')}] {reason}; high-risk approval "
                            "is required"
                        ),
                        level=PermissionLevel.DESTRUCTIVE,
                        needs_confirmation=True,
                        approval_required=True,
                        approval_scope=self._approval_scope_for(tool_call, high_risk=True),
                    )
                    record(result, rule_id=rule.get("id"))
                    return result

        # Classify known high-risk patterns before returning the more general
        # outside-workspace result. Otherwise ``read_file /etc/shadow`` would
        # be presented as an ordinary directory-boundary approval rather than
        # the high-risk credential/system-file access it actually is.
        file_result = self._check_file_paths(tool_call)
        if file_result is not None:
            file_result.level = level
            record(file_result)
            return file_result

        approval_required = self._requires_approval(tool_call)
        result = GuardResult(
            allowed=True,
            level=level,
            reason=f"Approval required for {tool_call.name}" if approval_required else "",
            approval_required=approval_required,
            approval_scope=(self._approval_scope_for(tool_call) if approval_required else None),
        )
        record(result, whitelisted=tool_whitelisted)
        return result
