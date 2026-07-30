from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import logging
import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

from engine.safety.approval import ApprovalScope
from engine.sandbox import ExecutionEnvironment, LocalExecutionEnvironment

from .interface import ToolCall, ToolDefinition, ToolResult
from .truncation import truncate_output

if TYPE_CHECKING:
    from engine.safety.tool_guard import ToolGuard

    from .ledger import ToolExecutionLedger

_TOOL_ALIASES = {
    "websearch": "web_search",
    "webfetch": "web_fetch",
}
# Values mirror engine.safety.tool_guard.PermissionLevel; kept as literals so
# the tool layer does not import the safety layer (which imports tool).
_VALID_PERMISSION_LEVELS = frozenset({"read", "write", "execute", "destructive"})
_VALID_APPROVAL_POLICIES = frozenset({"never", "policy", "always"})
_VALID_SIDE_EFFECTS = frozenset({"none", "write", "external", "destructive"})
_VALID_CONCURRENCY = frozenset({"safe", "serial"})
_VALID_EXECUTION_ENVIRONMENTS = frozenset({"host", "sandbox", "either"})
# Tool providers are executable server-side code.  Runtime setup may import
# only these shipped modules; the generic ``load_providers`` API intentionally
# remains available for explicit extension/test hosts.
_BUILTIN_PROVIDER_FILENAMES = frozenset(
    {
        "edit_file.py",
        "git_ops.py",
        "glob_files.py",
        "grep.py",
        "list_dir.py",
        "memory_ops.py",
        "read_file.py",
        "read_pdf.py",
        "render_pdf_page.py",
        "render_ui.py",
        "shell.py",
        "skill_load.py",
        "skill_manage.py",
        "todo.py",
        "web_crawl.py",
        "web_fetch.py",
        "web_search.py",
        "write_file.py",
    }
)
log = logging.getLogger(__name__)


def _meta_str_tuple(meta: dict, key: str, source: Path) -> tuple[str, ...]:
    """Read an optional list-of-strings field from TOOL_META, validating it."""
    value = meta.get(key, ())
    if isinstance(value, (list, tuple)) and all(
        isinstance(item, str) and item for item in value
    ):
        return tuple(value)
    raise ValueError(f"{source} TOOL_META.{key} must be a list of non-empty strings")


def _canonical_tool_name(name: str) -> str:
    return _TOOL_ALIASES.get(name, name)


def _is_async_callable(func: Callable) -> bool:
    return inspect.iscoroutinefunction(func) or inspect.iscoroutinefunction(
        getattr(func, "__call__", None)
    )


def _accepts_environment(func: Callable) -> bool:
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return "environment" in parameters


def _call_fingerprint(call: ToolCall) -> str:
    return json.dumps(
        {
            "id": call.id,
            "name": call.name,
            "arguments": call.arguments,
            "idempotency_key": call.idempotency_key,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolDefinition, Callable]] = {}
        self._enabled: set[str] | None = None
        self._execution_ledger: ToolExecutionLedger | None = None
        self._working_dir: Path | None = None
        self._host_environment: ExecutionEnvironment = LocalExecutionEnvironment()
        self._sandbox_environment: ExecutionEnvironment | None = None
        self._wants_environment: set[str] = set()
        self._serial_locks: dict[str, asyncio.Lock] = {}
        self._tool_guard: ToolGuard | None = None
        self._authorized_call: ContextVar[
            tuple[str, str | None, ApprovalScope | None] | None
        ] = ContextVar(
            f"agent_smith_authorized_tool_call_{id(self)}",
            default=None,
        )

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        func: Callable,
        *,
        hidden: bool = False,
        opaque_command: bool = False,
        network_access: bool = False,
        path_args: Sequence[str] = (),
        list_path_args: Sequence[str] = (),
        is_write_tool: bool = False,
        permission_level: str = "",
        approval_policy: str = "never",
        read_actions: Iterable[str] = (),
        timeout_seconds: float | None = None,
        retryable: bool = False,
        side_effect: str | None = None,
        idempotent: bool = False,
        concurrency: str = "safe",
        execution_environment: str = "host",
    ) -> None:
        if name in self._tools:
            raise ValueError(f"Duplicate tool registered: {name}")
        if not isinstance(hidden, bool):
            raise ValueError(f"Tool {name} hidden must be a boolean")
        if not isinstance(opaque_command, bool):
            raise ValueError(f"Tool {name} opaque_command must be a boolean")
        if not isinstance(network_access, bool):
            raise ValueError(f"Tool {name} network_access must be a boolean")
        if permission_level and permission_level not in _VALID_PERMISSION_LEVELS:
            raise ValueError(
                f"Tool {name} has invalid permission_level: {permission_level!r} "
                f"(expected one of {sorted(_VALID_PERMISSION_LEVELS)})"
            )
        if approval_policy not in _VALID_APPROVAL_POLICIES:
            raise ValueError(
                f"Tool {name} has invalid approval_policy: {approval_policy!r} "
                f"(expected one of {sorted(_VALID_APPROVAL_POLICIES)})"
            )
        if timeout_seconds is not None:
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or timeout_seconds <= 0
            ):
                raise ValueError(f"Tool {name} timeout_seconds must be a positive number")
            timeout_seconds = float(timeout_seconds)
        if not isinstance(retryable, bool):
            raise ValueError(f"Tool {name} retryable must be a boolean")
        resolved_side_effect = side_effect or ("write" if is_write_tool else "none")
        if resolved_side_effect not in _VALID_SIDE_EFFECTS:
            raise ValueError(
                f"Tool {name} side_effect must be one of {sorted(_VALID_SIDE_EFFECTS)}"
            )
        if (
            timeout_seconds is not None
            and resolved_side_effect != "none"
            and not _is_async_callable(func)
        ):
            raise ValueError(
                f"Tool {name} uses a synchronous side-effecting handler and cannot "
                "declare timeout_seconds; use an async handler with cooperative "
                "cancellation or omit the timeout"
            )
        if concurrency not in _VALID_CONCURRENCY:
            raise ValueError(
                f"Tool {name} concurrency must be one of {sorted(_VALID_CONCURRENCY)}"
            )
        if execution_environment not in _VALID_EXECUTION_ENVIRONMENTS:
            raise ValueError(
                f"Tool {name} execution_environment must be one of "
                f"{sorted(_VALID_EXECUTION_ENVIRONMENTS)}"
            )
        accepts_environment = _accepts_environment(func)
        if execution_environment != "host" and not accepts_environment:
            raise ValueError(
                f"Tool {name} declares execution_environment={execution_environment!r} "
                "and must accept an 'environment' parameter"
            )
        defn = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            hidden=hidden,
            opaque_command=opaque_command,
            network_access=network_access,
            path_args=tuple(path_args),
            list_path_args=tuple(list_path_args),
            is_write_tool=bool(is_write_tool or resolved_side_effect != "none"),
            permission_level=permission_level,
            approval_policy=approval_policy,
            read_actions=frozenset(read_actions),
            timeout_seconds=timeout_seconds,
            retryable=retryable,
            side_effect=resolved_side_effect,
            idempotent=bool(idempotent),
            concurrency=concurrency,
            execution_environment=execution_environment,
        )
        self._tools[name] = (defn, func)
        if accepts_environment:
            self._wants_environment.add(name)

    def load_providers(
        self,
        tools_dir: Path,
        *,
        allowed_filenames: frozenset[str] | None = None,
    ) -> None:
        """Auto-discover tool providers from a directory of .py files.

        Each file should define a TOOL_META dict and an execute function.
        TOOL_META keys: name, description, parameters (JSON Schema dict).
        Optional metadata keys: hidden, opaque_command, network_access, path_args, list_path_args,
        is_write_tool, permission_level, approval_policy, read_actions — propagated onto the
        ToolDefinition so safety modules can use them instead of hardcoded
        lookup tables.
        """
        if not tools_dir.is_dir():
            return
        if allowed_filenames is None:
            provider_files = sorted(tools_dir.glob("*.py"))
        else:
            provider_files = [
                tools_dir / filename
                for filename in sorted(allowed_filenames)
                if (tools_dir / filename).is_file()
            ]
        for py_file in provider_files:
            if py_file.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
                meta: dict[str, Any] = getattr(mod, "TOOL_META", {})
                execute_fn: Callable | None = getattr(mod, "execute", None)
                if meta and execute_fn:
                    name = meta.get("name")
                    if not isinstance(name, str) or not name:
                        raise ValueError(f"{py_file} has invalid TOOL_META.name")
                    if not callable(execute_fn):
                        raise ValueError(f"{py_file} execute is not callable")
                    parameters = meta.get("parameters", {})
                    if parameters and not isinstance(parameters, dict):
                        raise ValueError(f"{py_file} TOOL_META.parameters must be a dict")
                    permission_level = meta.get("permission_level", "")
                    if not isinstance(permission_level, str):
                        raise ValueError(f"{py_file} TOOL_META.permission_level must be a string")
                    self.register(
                        name=name,
                        description=meta.get("description", ""),
                        parameters=parameters,
                        func=execute_fn,
                        hidden=meta.get("hidden", False),
                        opaque_command=meta.get("opaque_command", False),
                        network_access=meta.get("network_access", False),
                        path_args=_meta_str_tuple(meta, "path_args", py_file),
                        list_path_args=_meta_str_tuple(meta, "list_path_args", py_file),
                        is_write_tool=bool(meta.get("is_write_tool", False)),
                        permission_level=permission_level,
                        approval_policy=meta.get("approval_policy", "never"),
                        read_actions=frozenset(_meta_str_tuple(meta, "read_actions", py_file)),
                        timeout_seconds=meta.get("timeout_seconds"),
                        retryable=meta.get("retryable", False),
                        side_effect=meta.get("side_effect"),
                        idempotent=meta.get("idempotent", False),
                        concurrency=meta.get("concurrency", "safe"),
                        execution_environment=meta.get("execution_environment", "host"),
                    )
            except ValueError as exc:
                # A contract violation — malformed TOOL_META, or a name already
                # taken by an earlier file — means this tool is simply absent from
                # the registry: the model never sees it and gets no error, and on a
                # duplicate the name silently stays with whichever file sorted
                # first.  Say so at ERROR with the consequence spelled out, rather
                # than burying it in a traceback among genuine import failures.
                log.error("Tool provider rejected, tool unavailable: %s (%s)", py_file.name, exc)
            except Exception:
                log.exception("Failed to load tool provider: %s", py_file)

    def load_builtin_providers(self, tools_dir: Path) -> None:
        """Load only shipped runtime provider modules from ``tools_dir``.

        A runtime directory is mutable project data in some deployments, so
        discovering every ``*.py`` file here would execute model-writable code
        in the server process on the next request.
        """
        self.load_providers(
            tools_dir,
            allowed_filenames=_BUILTIN_PROVIDER_FILENAMES,
        )

    def get_schemas(self, enabled: list[str] | None = None) -> list[dict]:
        """Return OpenAI-compatible tool schemas."""
        active = set(enabled) if enabled is not None else self._enabled
        result: list[dict] = []
        for name, (defn, _) in self._tools.items():
            if active is not None and name not in active:
                continue
            result.append({
                "type": "function",
                "function": {
                    "name": defn.name,
                    "description": defn.description,
                    "parameters": defn.parameters,
                },
        })
        return result

    def scoped_to(self, names: Iterable[str]) -> "ScopedToolRegistry":
        """Return a non-mutating, per-workflow capability view.

        The identity-wide allowlist remains the outer limit.  A pipeline node
        can only narrow it; it can never re-enable a tool that identity or
        profile configuration has disabled.
        """
        return ScopedToolRegistry(self, names)

    def unavailable_tool_message(self, name: str) -> str:
        """Explain why a tool absent from this registry view cannot run."""
        canonical = _canonical_tool_name(name)
        if canonical not in self._tools:
            return f"Unknown tool: {name}"
        return f"Tool disabled: {canonical}"

    def set_enabled(self, enabled: list[str] | None) -> list[str]:
        """Restrict visible/executable tools.

        Returns configured names that are not registered. Unknown names are
        intentionally not kept in the active set, so stale config cannot expose
        phantom tools to prompts or execution.
        """
        if enabled is None:
            self._enabled = None
            return []

        requested = [
            (name, _canonical_tool_name(name))
            for name in enabled
            if isinstance(name, str) and name
        ]
        known = set(self._tools)
        self._enabled = {canonical for _, canonical in requested if canonical in known}
        return [name for name, canonical in requested if canonical not in known]

    def list_tool_names(self, *, include_disabled: bool = False) -> list[str]:
        names = sorted(self._tools)
        if include_disabled or self._enabled is None:
            return names
        return [name for name in names if name in self._enabled]

    def list_visible_tool_names(
        self,
        *,
        include_disabled: bool = False,
        include_hidden: bool = False,
    ) -> list[str]:
        """List tool names visible to a model-facing default configuration."""
        names = self.list_tool_names(include_disabled=include_disabled)
        if include_hidden:
            return names
        return [name for name in names if not self._tools[name][0].hidden]

    def get(self, name: str) -> ToolDefinition | None:
        """Return a tool's public contract for policy enforcement."""
        entry = self._tools.get(_canonical_tool_name(name))
        return entry[0] if entry is not None else None

    def wrap_tool(self, name: str, wrapper: Callable[[Callable], Callable]) -> bool:
        """Replace a tool handler while preserving its public definition."""
        tool_name = _canonical_tool_name(name)
        entry = self._tools.get(tool_name)
        if entry is None:
            return False
        defn, func = entry
        self._tools[tool_name] = (defn, wrapper(func))
        return True

    def bind_execution_ledger(self, ledger: ToolExecutionLedger | None) -> None:
        """Bind a per-run ledger used to protect side-effecting tools."""
        self._execution_ledger = ledger

    def bind_tool_guard(self, guard: ToolGuard | None) -> None:
        """Install the runtime hard-safety backstop for direct executions."""
        self._tool_guard = guard

    @contextmanager
    def authorize_execution(
        self,
        call: ToolCall,
        *,
        approval_id: str | None = None,
        approval_scope: ApprovalScope | None = None,
    ) -> Iterator[None]:
        """Seal one normalized call after the runtime policy has approved it.

        The ReAct loop owns policy evaluation and any user-approval wait.  This
        short-lived permit lets :meth:`execute` distinguish that path from
        internal callers that accidentally invoke the registry directly.
        Approval-gated calls additionally require the broker-issued approval
        identifier; a matching call fingerprint alone is insufficient.
        """

        normalized = self.normalize_call(call)
        token = self._authorized_call.set(
            (_call_fingerprint(normalized), approval_id, approval_scope)
        )
        try:
            yield
        finally:
            self._authorized_call.reset(token)

    def bind_execution_environment(self, environment: ExecutionEnvironment | None) -> None:
        """Bind an execution backend without replacing the host backend.

        Tool definitions declare ``host``, ``sandbox``, or ``either``.  A
        sandbox binding augments the always-present host backend; it must
        never silently redirect host-only tools into the sandbox or vice versa.
        """
        if environment is None:
            self._sandbox_environment = None
        elif environment.name == "host":
            self._host_environment = environment
        elif environment.name == "sandbox":
            self._sandbox_environment = environment
        else:
            raise ValueError(f"Unsupported execution environment: {environment.name!r}")

    def _environment_for(
        self, required_environment: str
    ) -> ExecutionEnvironment | None:
        if required_environment == "host":
            return self._host_environment
        if required_environment == "sandbox":
            return self._sandbox_environment
        return self._sandbox_environment or self._host_environment

    def bind_working_directory(self, working_dir: str | Path | None) -> None:
        """Bind the root used for relative paths during one agent run."""
        self._working_dir = Path(working_dir).expanduser().resolve() if working_dir else None

    @property
    def working_directory(self) -> Path | None:
        """Resolved workspace bound for the current request, if any."""
        return self._working_dir

    def normalize_call(self, call: ToolCall) -> ToolCall:
        """Resolve declared paths against the bound project directory.

        This is deliberately performed before policy checks so the safety guard
        and the provider operate on the same canonical paths.  Optional path
        arguments whose schema default is a non-empty path (for example ``.``)
        are materialized before resolving: otherwise an omitted argument lets a
        provider silently fall back to the process working directory.
        """
        tool_name = _canonical_tool_name(call.name)
        if self._working_dir is None:
            if tool_name == call.name:
                return call
            return ToolCall(
                id=call.id,
                name=tool_name,
                arguments=dict(call.arguments),
                idempotency_key=call.idempotency_key,
            )

        entry = self._tools.get(tool_name)
        if entry is None:
            return call

        definition, _ = entry
        arguments = dict(call.arguments)
        properties = (
            definition.parameters.get("properties")
            if isinstance(definition.parameters, dict)
            else None
        )
        if isinstance(properties, dict) and "cwd" in properties and not arguments.get("cwd"):
            arguments["cwd"] = str(self._working_dir)

        for argument_name in definition.path_args:
            value = arguments.get(argument_name)
            if not isinstance(value, str) or not value:
                parameter = properties.get(argument_name) if isinstance(properties, dict) else None
                default = parameter.get("default") if isinstance(parameter, dict) else None
                if not isinstance(default, str) or not default:
                    continue
                value = default
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = self._working_dir / candidate
            arguments[argument_name] = str(candidate.resolve())

        return ToolCall(
            id=call.id,
            name=tool_name,
            arguments=arguments,
            idempotency_key=call.idempotency_key,
        )

    async def _invoke(
        self,
        func: Callable,
        arguments: dict,
        timeout_seconds: float | None,
    ) -> Any:
        async def invoke() -> Any:
            if _is_async_callable(func):
                result = func(**arguments)
            else:
                result = await asyncio.to_thread(func, **arguments)
            return await result if inspect.isawaitable(result) else result

        operation = invoke()
        if timeout_seconds is None:
            return await operation
        return await asyncio.wait_for(operation, timeout_seconds)

    @staticmethod
    def _finalize_result(result: ToolResult, tool_name: str) -> ToolResult:
        return ToolResult(
            call_id=result.call_id,
            content=truncate_output(result.content, tool_name=tool_name),
            is_error=result.is_error,
            error_kind=result.error_kind,
            retryable=result.retryable,
            timed_out=result.timed_out,
            side_effect_status=result.side_effect_status,
            metadata=dict(result.metadata),
        )

    async def _execute_registered(
        self,
        call: ToolCall,
        tool_name: str,
        defn: ToolDefinition,
        func: Callable,
        environment: ExecutionEnvironment,
    ) -> ToolResult:
        ledger = self._execution_ledger if defn.side_effect != "none" else None
        idempotency_key = call.idempotency_key or (
            ledger.idempotency_key_for(
                call_id=call.id,
                tool_name=tool_name,
                arguments=call.arguments,
            )
            if ledger is not None
            else call.id
        )
        claimed = False
        if ledger is not None:
            decision = ledger.begin(
                call_id=call.id,
                tool_name=tool_name,
                idempotency_key=idempotency_key,
                idempotent=defn.idempotent,
            )
            if decision.result is not None:
                return decision.result
            claimed = decision.claimed

        arguments = call.arguments
        if tool_name in self._wants_environment:
            # Injected last so a model-supplied "environment" argument can
            # never smuggle a fake environment past the binding.
            arguments = {**call.arguments, "environment": environment}

        try:
            content = await self._invoke(func, arguments, defn.timeout_seconds)
            content_text = str(content)
            is_error = _looks_like_tool_error(content_text)
            result = ToolResult(
                call_id=call.id,
                content=content_text,
                is_error=is_error,
                error_kind="provider_error" if is_error else None,
                retryable=defn.retryable if is_error else False,
                side_effect_status=(
                    "unknown" if is_error and defn.side_effect != "none"
                    else "completed" if defn.side_effect != "none"
                    else "none"
                ),
            )
        except asyncio.TimeoutError:
            result = ToolResult(
                call_id=call.id,
                content=f"Tool timed out after {defn.timeout_seconds:g}s",
                is_error=True,
                error_kind="timeout",
                retryable=defn.retryable,
                timed_out=True,
                side_effect_status="unknown" if defn.side_effect != "none" else "none",
            )
        except Exception as exc:
            result = ToolResult(
                call_id=call.id,
                content=str(exc),
                is_error=True,
                error_kind="exception",
                retryable=defn.retryable,
                side_effect_status="unknown" if defn.side_effect != "none" else "none",
            )

        result = await asyncio.to_thread(self._finalize_result, result, tool_name)
        if ledger is not None and claimed:
            ledger.finish(
                call_id=call.id,
                idempotency_key=idempotency_key,
                result=result,
            )
        return result

    async def execute(self, call: ToolCall) -> ToolResult:
        # ``execute`` is also used by a few internal integrations and tests
        # directly.  Normalize here as a backstop so those callers cannot skip
        # the path binding enforced by the ReAct loop.
        call = self.normalize_call(call)
        tool_name = _canonical_tool_name(call.name)

        entry = self._tools.get(tool_name)
        if entry is None:
            return ToolResult(
                call_id=call.id,
                content=f"Unknown tool: {call.name}",
                is_error=True,
                error_kind="unknown_tool",
            )

        if self._enabled is not None and tool_name not in self._enabled:
            return ToolResult(
                call_id=call.id,
                content=f"Tool disabled: {tool_name}",
                is_error=True,
                error_kind="tool_disabled",
            )

        defn, func = entry
        approval_granted = False
        approval_scope: ApprovalScope | None = None
        if self._tool_guard is not None:
            # The ReAct loop already evaluated and audited this call; this is a
            # backstop for internal callers, so it must not log a second time.
            decision = self._tool_guard.check(call, audit=False)
            authorization = self._authorized_call.get()
            authorized = (
                authorization is not None
                and authorization[0] == _call_fingerprint(call)
            )
            approval_granted = (
                authorized
                and bool(authorization[1])
                and authorization[2] == decision.approval_scope
            )
            approval_scope = decision.approval_scope
            # A project-boundary result is intentionally ``allowed=False``
            # until the approval broker grants the exact normalized call.  Do
            # not turn a bare approval id into a general bypass: the
            # fingerprint check above binds this exception to one call only.
            blocked_by_policy = not decision.allowed and not (
                decision.approval_required and approval_granted
            )
            approval_required = decision.approval_required and not approval_granted
            if blocked_by_policy or approval_required:
                reason = decision.reason or (
                    f"Approval required for {tool_name}"
                    if approval_required
                    else f"Tool blocked by policy: {tool_name}"
                )
                return ToolResult(
                    call_id=call.id,
                    content=f"[BLOCKED] {reason}",
                    is_error=True,
                    error_kind=(
                        "policy_blocked" if blocked_by_policy else "approval_required"
                    ),
                    metadata={
                        "blocked": True,
                        "approval_required": approval_required,
                        "level": decision.level.value,
                    },
                )

        required_env = defn.execution_environment
        environment = self._environment_for(required_env)
        if environment is None:
            return ToolResult(
                call_id=call.id,
                content=(
                    f"Error: tool '{tool_name}' requires the '{required_env}' execution "
                    "environment, but no matching backend is bound"
                ),
                is_error=True,
                error_kind="environment_unavailable",
            )
        if (
            approval_granted
            and approval_scope is not None
            and approval_scope.grants_host_execution
        ):
            scoped_environment = getattr(environment, "with_approval_scope", None)
            if not callable(scoped_environment):
                return ToolResult(
                    call_id=call.id,
                    content=(
                        "Error: the bound sandbox cannot apply the approved host "
                        "access scope"
                    ),
                    is_error=True,
                    error_kind="environment_unavailable",
                )
            environment = scoped_environment(approval_scope)
        if defn.concurrency == "serial":
            lock = self._serial_locks.setdefault(tool_name, asyncio.Lock())
            async with lock:
                return await self._execute_registered(
                    call,
                    tool_name,
                    defn,
                    func,
                    environment,
                )
        return await self._execute_registered(
            call,
            tool_name,
            defn,
            func,
            environment,
        )

    def list_tools(self) -> list[ToolDefinition]:
        if self._enabled is None:
            return [defn for defn, _ in self._tools.values()]
        return [defn for name, (defn, _) in self._tools.items() if name in self._enabled]

    def definitions(self) -> dict[str, ToolDefinition]:
        """All registered definitions (incl. disabled) keyed by name, for safety guards."""
        return {name: defn for name, (defn, _) in self._tools.items()}


class ScopedToolRegistry:
    """A non-mutating view that narrows one ``ToolRegistry`` for a skill node."""

    def __init__(self, registry: ToolRegistry, names: Iterable[str]) -> None:
        self._registry = registry
        self._allowed = frozenset(
            _canonical_tool_name(name)
            for name in names
            if isinstance(name, str) and name
        )

    def _active_names(self) -> set[str]:
        # ``list_tool_names`` already applies the parent identity/profile
        # allowlist.  Intersecting here prevents a scoped view from widening
        # that outer boundary through ``get_schemas(enabled=...)``.
        return self._allowed.intersection(self._registry.list_tool_names())

    @property
    def working_directory(self) -> Path | None:
        return self._registry.working_directory

    def get_schemas(self, enabled: list[str] | None = None) -> list[dict]:
        active = self._active_names()
        if enabled is not None:
            active.intersection_update(
                _canonical_tool_name(name)
                for name in enabled
                if isinstance(name, str) and name
            )
        return self._registry.get_schemas(sorted(active))

    def list_tools(self) -> list[ToolDefinition]:
        active = self._active_names()
        return [
            tool
            for tool in self._registry.list_tools()
            if tool.name in active
        ]

    def definitions(self) -> dict[str, ToolDefinition]:
        active = self._active_names()
        return {
            name: definition
            for name, definition in self._registry.definitions().items()
            if name in active
        }

    def get(self, name: str) -> ToolDefinition | None:
        canonical = _canonical_tool_name(name)
        if canonical not in self._active_names():
            return None
        return self._registry.get(canonical)

    def unavailable_tool_message(self, name: str) -> str:
        """Keep an out-of-scope call out of policy and approval handling."""
        canonical = _canonical_tool_name(name)
        if canonical not in self._registry.list_tool_names():
            return self._registry.unavailable_tool_message(name)
        return f"Tool disabled for this pipeline node: {canonical}"

    def normalize_call(self, call: ToolCall) -> ToolCall:
        return self._registry.normalize_call(call)

    @contextmanager
    def authorize_execution(
        self,
        call: ToolCall,
        *,
        approval_id: str | None = None,
        approval_scope: ApprovalScope | None = None,
    ) -> Iterator[None]:
        with self._registry.authorize_execution(
            call,
            approval_id=approval_id,
            approval_scope=approval_scope,
        ):
            yield

    async def execute(self, call: ToolCall) -> ToolResult:
        call = self._registry.normalize_call(call)
        if _canonical_tool_name(call.name) not in self._active_names():
            return ToolResult(
                call_id=call.id,
                content=self.unavailable_tool_message(call.name),
                is_error=True,
                error_kind="tool_disabled",
            )
        return await self._registry.execute(call)


_EXIT_CODE_RE = re.compile(r"^\[exit_code=(-?\d+)\]")


def _looks_like_tool_error(content: str) -> bool:
    """Detect provider-level failures returned as text.

    Tool providers historically returned strings instead of ToolResult. Treat
    the common failure prefixes as errors so the ReAct loop can recover.
    """

    stripped = content.lstrip()
    if stripped.startswith((
        "Error:",
        "HTTP Error:",
        "URL Error:",
        "[BLOCKED]",
        "Memory rejected:",
    )):
        return True

    match = _EXIT_CODE_RE.match(stripped)
    return bool(match and int(match.group(1)) != 0)
