"""Request-scoped fact-forcing preflight for state-changing tool calls.

The behavior is inspired by ECC GateGuard's MIT-licensed fact-forcing hook:
https://github.com/affaan-m/ECC

This module deliberately does not enforce hard safety boundaries. It challenges
the first state-changing operation in a turn so the model investigates and
retries; :mod:`engine.safety.tool_guard` remains the non-bypassable guard.
"""

from __future__ import annotations

import os
import re
import shlex
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from engine.tool.interface import ToolCall, ToolDefinition


_DISABLE_VALUES = frozenset({"0", "false", "off", "disabled", "disable", "no"})
_CURRENT_FACT_GATE: ContextVar[FactGate | None] = ContextVar(
    "agent_smith_fact_gate",
    default=None,
)
_STATE_CHANGING_SHELL_KEY = "__state_changing_shell__"
_SHELL_OPERATORS = frozenset({";", "&", "&&", "||", "|"})
_SHELL_REDIRECTIONS = frozenset({">", ">>", "<", "<<", "<<<", "&>", "2>", "2>>"})
_READ_ONLY_COMMANDS = frozenset({
    "[",
    "cat",
    "cmp",
    "command",
    "date",
    "df",
    "diff",
    "du",
    "echo",
    "file",
    "grep",
    "head",
    "ls",
    "pwd",
    "rg",
    "sort",
    "stat",
    "tail",
    "test",
    "tree",
    "true",
    "type",
    "uname",
    "uniq",
    "wc",
    "whereis",
    "which",
})
_READ_ONLY_GIT_SUBCOMMANDS = frozenset({
    "blame",
    "diff",
    "grep",
    "log",
    "ls-files",
    "ls-tree",
    "rev-parse",
    "show",
    "status",
})
_STRUCTURED_READ_ACTIONS: dict[str, frozenset[str]] = {
    "git_ops": frozenset({"diff", "discover", "status"}),
    "memory_ops": frozenset({"search"}),
    "skill_manage": frozenset({"get", "list", "versions"}),
}


@dataclass(frozen=True)
class FactGateContext:
    """Identity and instruction for one user turn."""

    session_id: str
    turn_id: str


@dataclass(frozen=True)
class FactGateResult:
    challenged: bool
    reason: str = ""


class FactGate:
    """Challenge the first write to each file and first mutating shell per turn."""

    def __init__(
        self,
        context: FactGateContext,
        *,
        enabled: bool | None = None,
        tool_registry: dict[str, ToolDefinition] | None = None,
    ) -> None:
        self.context = context
        self.enabled = _enabled_from_env() if enabled is None else enabled
        self._scope = f"{context.session_id}:{context.turn_id}"
        self._checked: set[str] = set()
        self._pending: set[str] = set()
        self._tool_registry: dict[str, ToolDefinition] = tool_registry or {}

    def begin_round(self) -> None:
        """Make prior-round challenges retryable without opening the current round."""

        self._checked.update(self._pending)
        self._pending.clear()

    def _resolve_read_actions(self, tool_name: str) -> frozenset[str] | None:
        """Return the read-only actions for *tool_name*, or ``None``.

        Tool-declared metadata wins; falls back to ``_STRUCTURED_READ_ACTIONS``.
        """
        defn = self._tool_registry.get(tool_name)
        if defn is not None and defn.read_actions:
            return defn.read_actions
        return _STRUCTURED_READ_ACTIONS.get(tool_name)

    def _resolve_write_path_args(self, tool_name: str) -> tuple[str, ...]:
        """Return the path argument names when *tool_name* writes files.

        Tool-declared metadata wins; falls back to the built-in file tools.
        Returns ``()`` for tools that do not write files.
        """
        defn = self._tool_registry.get(tool_name)
        if defn is not None and defn.is_write_tool and defn.path_args:
            return defn.path_args
        if tool_name in {"edit_file", "write_file"}:
            return ("path", "file_path")
        return ()

    def evaluate(self, call: ToolCall) -> FactGateResult:
        if not self.enabled:
            return FactGateResult(False)

        write_path_args = self._resolve_write_path_args(call.name)
        if write_path_args:
            raw_path = next(
                (call.arguments.get(arg) for arg in write_path_args if call.arguments.get(arg)),
                None,
            )
            if not raw_path:
                return FactGateResult(False)
            path = _normalize_path(str(raw_path))
            key = f"{self._scope}:file:{path}"
            if key in self._checked:
                return FactGateResult(False)
            self._pending.add(key)
            return FactGateResult(True, _file_challenge(call.name, path))

        if call.name == "shell":
            command = str(call.arguments.get("command") or "").strip()
            if not command or _is_read_only_shell(command):
                return FactGateResult(False)
            key = f"{self._scope}:{_STATE_CHANGING_SHELL_KEY}"
            if key in self._checked:
                return FactGateResult(False)
            self._pending.add(key)
            return FactGateResult(True, _shell_challenge(command))

        read_actions = self._resolve_read_actions(call.name)
        if read_actions is not None:
            action = str(call.arguments.get("action") or "").strip().lower()
            if action in read_actions:
                return FactGateResult(False)
            key = f"{self._scope}:structured:{call.name}"
            if key in self._checked:
                return FactGateResult(False)
            self._pending.add(key)
            return FactGateResult(True, _structured_tool_challenge(call.name, action))

        return FactGateResult(False)


def current_fact_gate() -> FactGate | None:
    return _CURRENT_FACT_GATE.get()


@contextmanager
def use_fact_gate(gate: FactGate | None) -> Iterator[None]:
    """Bind a gate to the current async request and restore the prior value."""

    token = _CURRENT_FACT_GATE.set(gate)
    try:
        yield
    finally:
        _CURRENT_FACT_GATE.reset(token)


def _enabled_from_env() -> bool:
    raw = os.getenv("AGENT_SMITH_FACT_GATE", "on").strip().lower()
    return raw not in _DISABLE_VALUES


def _normalize_path(raw_path: str) -> str:
    sanitized = "".join(ch for ch in raw_path if ch >= " " and ch not in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
    path = Path(sanitized).expanduser()
    return os.path.normpath(str(path))


def _file_challenge(tool_name: str, path: str) -> str:
    action = "editing" if tool_name == "edit_file" else "writing or creating"
    return "\n".join([
        "[Fact-Forcing Gate]",
        f"Before {action} {path}, present these facts:",
        "1. List ALL files that import, require, or call this file.",
        "2. List the public functions/classes affected by this change.",
        "3. If it reads or writes data, show the relevant data schema, field names, and formats using redacted or synthetic values.",
        "4. Quote the user's current instruction verbatim.",
        "Present the facts, then retry the same operation.",
    ])


def _shell_challenge(command: str) -> str:
    safe_command = " ".join(command.replace("\r", " ").replace("\n", " ").split())
    if len(safe_command) > 240:
        safe_command = safe_command[:237] + "..."
    return "\n".join([
        "[Fact-Forcing Gate]",
        f"Before the first shell command this turn that changes or produces state ({safe_command}), present:",
        "1. The current user request and what this command verifies, changes, or produces.",
        "2. The files, data, dependencies, or external state it may affect.",
        "3. A one-line rollback procedure when the operation is mutating.",
        "4. Quote the user's current instruction verbatim.",
        "Present the facts, then retry the operation.",
    ])


def _structured_tool_challenge(tool_name: str, action: str) -> str:
    safe_action = "".join(ch for ch in action if ch.isalnum() or ch in "._-") or "unknown"
    return "\n".join([
        "[Fact-Forcing Gate]",
        f"Before {tool_name}.{safe_action} changes state, present:",
        "1. The current user request and why this action is necessary.",
        "2. The files, data, repository state, or persistent Agent state it may affect.",
        "3. A one-line rollback procedure.",
        "4. Quote the user's current instruction verbatim.",
        "Present the facts, then retry the operation.",
    ])


def _is_read_only_shell(command: str) -> bool:
    """Return true only for a narrow, composable introspection allowlist."""

    if "\n" in command or "\r" in command or "$(" in command or "`" in command:
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False

    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SHELL_REDIRECTIONS or re.fullmatch(r"\d*(?:>|>>)", token):
            return False
        if token in _SHELL_OPERATORS:
            if not segments[-1]:
                return False
            segments.append([])
            continue
        if re.fullmatch(r"[;&|<>]+", token):
            return False
        segments[-1].append(token)

    if not segments or not segments[-1]:
        return False
    return all(_is_read_only_segment(segment) for segment in segments)


def _is_read_only_segment(tokens: list[str]) -> bool:
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        name = tokens[0].split("=", 1)[0]
        if name not in {"COLUMNS", "LANG", "LC_ALL", "NO_COLOR", "TERM", "TZ"}:
            return False
        tokens = tokens[1:]
    if not tokens:
        return False

    raw_command = tokens[0]
    if "/" in raw_command:
        normalized = Path(os.path.normpath(raw_command))
        trusted_dirs = {
            Path("/bin"),
            Path("/opt/homebrew/bin"),
            Path("/sbin"),
            Path("/usr/bin"),
            Path("/usr/local/bin"),
            Path("/usr/sbin"),
        }
        if not normalized.is_absolute() or normalized.parent not in trusted_dirs:
            return False
    command = Path(raw_command).name
    args = tokens[1:]
    if command == "git":
        return _is_read_only_git(args)
    if command == "find":
        mutating_actions = {
            "-delete",
            "-exec",
            "-execdir",
            "-fls",
            "-fprintf",
            "-fprintf0",
            "-fprint",
            "-fprint0",
            "-ok",
            "-okdir",
        }
        return not any(arg in mutating_actions for arg in args)
    if command == "sed":
        return _is_read_only_sed(args)
    if command == "sort":
        return not any(
            arg == "-o"
            or arg.startswith("-o")
            or arg == "--output"
            or arg.startswith("--output=")
            or arg == "--compress-program"
            or arg.startswith("--compress-program=")
            for arg in args
        )
    if command == "tree":
        return not any(
            arg == "-o" or arg.startswith("-o") or arg == "--output" or arg.startswith("--output=")
            for arg in args
        )
    if command == "uniq":
        positional = [arg for arg in args if not arg.startswith("-")]
        return len(positional) <= 1
    if command == "command":
        return len(args) >= 1 and args[0] in {"-v", "-V"}
    if command == "date":
        return not any(
            arg == "-s" or arg.startswith("-s") or arg == "--set" or arg.startswith("--set=")
            for arg in args
        ) and all(arg.startswith(("-", "+")) for arg in args)
    return command in _READ_ONLY_COMMANDS


def _is_read_only_git(args: list[str]) -> bool:
    index = 0
    while index < len(args):
        token = args[index]
        if token == "-c":
            return False
        if token in {"-C", "--git-dir", "--work-tree"}:
            index += 2
            continue
        if token.startswith("--git-dir=") or token.startswith("--work-tree="):
            index += 1
            continue
        if token in {
            "--glob-pathspecs",
            "--icase-pathspecs",
            "--literal-pathspecs",
            "--no-pager",
            "--noglob-pathspecs",
        }:
            index += 1
            continue
        if token.startswith("-"):
            return False
        break
    if index >= len(args):
        return False

    subcommand = args[index]
    sub_args = args[index + 1 :]
    if any(
        arg in {"-o", "--output"} or arg.startswith("--output=")
        for arg in sub_args
    ):
        return False
    external_execution_flags = (
        "--ext-diff",
        "--open-files-in-pager",
        "--textconv",
    )
    if any(
        arg == flag or arg.startswith(f"{flag}=")
        for arg in sub_args
        for flag in external_execution_flags
    ):
        return False
    if any(arg == "-O" or arg.startswith("-O") for arg in sub_args):
        return False
    if subcommand in _READ_ONLY_GIT_SUBCOMMANDS:
        return True
    if subcommand == "branch":
        if not sub_args:
            return True
        mutating_flags = {
            "--copy",
            "--delete",
            "--edit-description",
            "--move",
            "--set-upstream-to",
            "--unset-upstream",
            "-C",
            "-D",
            "-M",
            "-c",
            "-d",
            "-m",
        }
        if any(arg in mutating_flags for arg in sub_args):
            return False
        if sub_args[0] in {"--list", "-l"}:
            return True
        read_only_flags = {
            "--all",
            "--color",
            "--no-color",
            "--no-column",
            "--remotes",
            "--show-current",
            "-a",
            "-r",
            "-v",
            "-vv",
        }
        read_only_prefixes = ("--color=", "--format=", "--sort=")
        return all(
            arg in read_only_flags or arg.startswith(read_only_prefixes)
            for arg in sub_args
        )
    return False


def _is_read_only_sed(args: list[str]) -> bool:
    if any(arg.startswith("-i") or arg.startswith("--in-place") for arg in args):
        return False
    scripts = [arg for arg in args if arg not in {"-n", "--quiet", "--silent", "-e", "--expression"}]
    if not scripts:
        return False
    # The first positional is the script (subsequent positionals are input
    # files). A script may contain ``;``/newline-separated sub-commands; every
    # one of them must be a read-only print command for the whole call to be
    # treated as read-only, otherwise a hidden write command (e.g. ``w``)
    # could slip through.
    pattern = re.compile(r"(?:\d+|\$)(?:,(?:\d+|\$))?p")
    sub_cmds = [part.strip() for part in re.split(r"[;\n]", scripts[0]) if part.strip()]
    if not sub_cmds:
        return False
    return all(pattern.fullmatch(cmd) for cmd in sub_cmds)
