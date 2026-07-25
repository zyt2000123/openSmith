"""Agent loop — routing, lifecycle, and entry points.

This is the top-level orchestrator. It does NOT execute pipelines or run
ReAct loops directly; it delegates to pipeline.py and react_loop.py.

Responsibilities:
  1. prepare_runtime()  — load tools, skills, memory, assemble prompt, route
  2. run_agent_stream() — route to DIRECT / pipeline / forced-skill
  3. Lifecycle          — persistence, cleanup, terminal state
  4. Entry points       — run_stream_with_runtime, reply_with_runtime, etc.
"""

from __future__ import annotations

from pathlib import Path
import asyncio
import inspect
import logging
import sys
from hashlib import sha256
from typing import AsyncGenerator, NamedTuple, Sequence
from uuid import uuid4

from engine.identity_catalog import IdentityCatalog, IdentitySpec, RouteDecision
from engine.llm.observability import generation_context, llm_purpose
from engine.llm.port import LLMPort
from engine.observability import (
    EventType,
    ExecutionEvent,
    RunObservation,
    RunObservationContext,
    raw_text_delta,
)
from engine.context import PromptAssembler, prompt_budget_for_llm
from engine.react_budget import DEFAULT_MAX_REACT_ITERS
from engine.safety.fact_gate import FactGate, FactGateContext, use_fact_gate
from engine.safety.tool_guard import ToolGuard
from engine.sandbox import MacOSSeatbeltEnvironment
from engine.skill.executor import execute_skill_events
from engine.skill.registry import SkillRegistry
from engine.tool.registry import ToolRegistry
from engine.execution.pipeline.backtrack import FailureLoopGuard
from engine.execution.pipeline.pipeline import run_pipeline
from engine.execution.pipeline.pipeline_context import (
    CTX_AGENT_ID,
    CTX_FORCED_SKILL,
    CTX_IDENTITY_ID,
    CTX_ROUTE_ID,
    CTX_SESSION_ID,
    CTX_STATE_DIR,
    CTX_TASK_TYPE,
    CTX_USER_MESSAGE,
    CTX_WORKING_DIR,
)
from engine.execution.react.react_loop import (
    IncompleteAgentRunError,
    react_event_loop,
)
from .run_state import RunStateError, RunStateStore, RunStatus, project_execution_event
from .run_stream import AgentRunStream
from .runtime import EngineRequest, EngineResult, RuntimeContext, RuntimeServices
from engine.execution.runtime_control import initial_runtime_control_prompt
from engine.execution.pipeline.skill_chain import SkillChain, load_gate_content
from engine.execution.tool_execution.tool_ledger import ToolExecutionLedger
from engine.safety.eval_guard import EVAL_SENSITIVE_GUIDANCE, detect_eval_sensitive
from engine.safety.approval import APPROVAL_BROKER, use_approval_context
from engine.execution.routing.task_router import route_task

# ReAct loop implementations belong to react_loop.py and are intentionally
# not re-exported from this orchestration module.
__all__ = (
    "prepare_runtime",
    "run_agent_stream",
    "run_stream_with_runtime",
    "resume_stream_with_runtime",
    "reply_with_runtime",
    "reply_events_with_runtime",
    "reply_stream_with_runtime",
    "run_memory_idle_tick",
    "run_memory_daily_tick",
)

logger = logging.getLogger(__name__)
_RUNTIME_LEARNING_TIMEOUT_SECONDS = 30.0

# ---------------------------------------------------------------------------
# Core: routing + dispatch
# ---------------------------------------------------------------------------


async def run_agent_stream(
    llm: LLMPort,
    system_prompt: str,
    user_message: str,
    tool_registry: ToolRegistry,
    skill_registry: SkillRegistry,
    route: RouteDecision,
    skill_chain: SkillChain | None,
    guard: FailureLoopGuard,
    tool_guard: ToolGuard | None = None,
    max_react_iters: int = DEFAULT_MAX_REACT_ITERS,
    history: list[dict] | None = None,
    forced_skill: str | None = None,
    execution_context: dict | None = None,
    gate_llm: LLMPort | None = None,
    disabled_skill_names: frozenset[str] = frozenset(),
    vision_messages: Sequence[dict] = (),
) -> AsyncGenerator[ExecutionEvent, None]:
    """Route to the right execution path and yield events."""
    if forced_skill:
        async for event in _run_forced_skill_stream(
            llm, system_prompt, tool_registry, skill_registry,
            user_message, forced_skill, tool_guard, max_react_iters,
            history=history, execution_context=execution_context,
            vision_messages=vision_messages,
        ):
            yield event
        return

    # Images sit in their own message just before the user turn. OpenAI accepts
    # consecutive user messages, and Anthropic's adapter merges same-role
    # neighbours into one turn — so both providers end up with image-then-text
    # without the engine ever turning ``user_message`` into a content list.
    base_messages = [
        {"role": "system", "content": system_prompt},
        *(history or []),
        *vision_messages,
        {"role": "user", "content": user_message},
    ]

    yield ExecutionEvent(EventType.ROUTE_DECIDED, route.to_event_data())

    if route.pipeline_id is None or skill_chain is None:
        async for event in react_event_loop(llm, base_messages, tool_registry, tool_guard, max_react_iters):
            yield event
        yield ExecutionEvent(EventType.DONE, {})
        return

    # A workflow's gates describe contracts for concrete skills. Running a
    # missing step as generic ReAct makes the contract invisible to the model
    # and can burn several full ReAct attempts before the gate blocks the run.
    # Treat an incompletely installed pipeline as unavailable and retain the
    # direct-agent path instead. User-disabled nodes remain an intentional
    # skip and therefore do not trigger this fallback.
    missing_skills = sorted({
        node.skill_name
        for node in skill_chain.nodes
        if node.skill_name not in disabled_skill_names and skill_registry.get(node.skill_name) is None
    })
    if missing_skills:
        logger.warning(
            "pipeline %r unavailable because skills are not installed: %s; falling back to direct ReAct",
            route.pipeline_id,
            ", ".join(missing_skills),
        )
        async for event in react_event_loop(llm, base_messages, tool_registry, tool_guard, max_react_iters):
            yield event
        yield ExecutionEvent(EventType.DONE, {})
        return

    context: dict = {
        CTX_USER_MESSAGE: user_message,
        CTX_IDENTITY_ID: route.identity_id,
        CTX_ROUTE_ID: route.route_id,
    }
    if execution_context:
        context.update({k: v for k, v in execution_context.items() if v is not None})

    context, start_node_idx = _apply_crash_checkpoint(
        context, route.route_id or "", user_message, len(skill_chain.nodes),
    )

    async for event in run_pipeline(
        skill_chain, llm, user_message, base_messages,
        tool_registry, skill_registry, tool_guard, guard,
        max_react_iters, context, gate_llm=gate_llm,
        start_node_idx=start_node_idx,
        disabled_skill_names=disabled_skill_names,
        vision_messages=vision_messages,
    ):
        yield event


def _apply_crash_checkpoint(
    context: dict,
    route_id: str,
    user_message: str,
    node_count: int,
) -> tuple[dict, int]:
    """Consume a crash-leftover checkpoint: resume the same request, drop stale ones.

    Every terminal path clears its checkpoint, so a surviving file means the
    process died mid-chain. Resume only when the identical request comes back
    in the same agent, identity, and working directory; anything else is a
    new task and the stale file is removed so it never masquerades as
    resumable state.
    """
    session_id = str(context.get(CTX_SESSION_ID) or "")
    state_dir = str(context.get(CTX_STATE_DIR) or "")
    if not session_id or not state_dir:
        return context, 0
    expected_agent_id = str(context.get(CTX_AGENT_ID) or "")
    expected_identity_id = str(context.get(CTX_IDENTITY_ID) or "")
    expected_working_dir = str(context.get(CTX_WORKING_DIR) or "")
    try:
        from engine.execution.pipeline.checkpoint import SessionStateManager

        manager = SessionStateManager(Path(state_dir))
        checkpoint = manager.restore(session_id)
        if checkpoint is None:
            return context, 0
        if (
            expected_agent_id
            and expected_identity_id
            and expected_working_dir
            and checkpoint.agent_id == expected_agent_id
            and checkpoint.identity_id == expected_identity_id
            and checkpoint.working_dir == expected_working_dir
            and checkpoint.route_id == route_id
            and checkpoint.context.get(CTX_USER_MESSAGE) == user_message
            and 0 <= checkpoint.skill_chain_index < node_count
        ):
            logger.info(
                "session %s: resuming crashed chain, skipping %d completed node(s)",
                session_id, checkpoint.skill_chain_index + 1,
            )
            return {**checkpoint.context, **context}, checkpoint.skill_chain_index + 1
        manager.clear(session_id)
    except Exception:
        logger.exception("failed to inspect crash checkpoint; starting fresh")
    return context, 0


# ---------------------------------------------------------------------------
# Forced skill execution
# ---------------------------------------------------------------------------


async def _run_forced_skill_stream(
    llm: LLMPort,
    system_prompt: str,
    tool_registry: ToolRegistry,
    skill_registry: SkillRegistry,
    user_message: str,
    forced_skill: str,
    tool_guard: ToolGuard | None,
    max_react_iters: int,
    history: list[dict] | None = None,
    execution_context: dict | None = None,
    vision_messages: Sequence[dict] = (),
) -> AsyncGenerator[ExecutionEvent, None]:
    yield ExecutionEvent(EventType.ROUTE_DECIDED, {"type": "skill", "skill": forced_skill})

    skill = skill_registry.get(forced_skill)
    if skill is None:
        msg = _missing_skill_message(skill_registry, forced_skill)
        yield ExecutionEvent(EventType.BLOCKED, {"skill": forced_skill, "reason": msg})
        yield ExecutionEvent(EventType.TEXT_DELTA, {"text": msg})
        yield ExecutionEvent(EventType.DONE, {})
        return

    yield ExecutionEvent(EventType.SKILL_START, {"skill": forced_skill, "index": 0})
    messages = [
        {"role": "system", "content": system_prompt},
        *(history or []),
        *vision_messages,
        {"role": "user", "content": user_message},
    ]
    context: dict = {CTX_USER_MESSAGE: user_message, CTX_TASK_TYPE: "skill", CTX_FORCED_SKILL: forced_skill}
    if execution_context:
        context.update({k: v for k, v in execution_context.items() if v is not None})
    output_parts: list[str] = []
    output_was_streamed = False
    terminal_type: str | None = None
    async for event in execute_skill_events(
        skill, llm, tool_registry, messages, context,
        max_react_iters, tool_guard=tool_guard,
        react_event_loop_fn=react_event_loop,
    ):
        if event.type == EventType.TEXT_DELTA:
            output_parts.append(str(event.data.get("text", "")))
            output_was_streamed = output_was_streamed or bool(event.data.get("already_streamed"))
            continue
        elif event.type == EventType.INCOMPLETE:
            terminal_type = "incomplete"
        elif event.type == EventType.FAILED:
            terminal_type = "failed"
        yield event
    if terminal_type:
        yield ExecutionEvent(EventType.SKILL_END, {"skill": forced_skill, "status": terminal_type})
        if output_parts:
            data: dict[str, object] = {"text": "".join(output_parts)}
            if output_was_streamed:
                data["already_streamed"] = True
            yield ExecutionEvent(EventType.TEXT_DELTA, data)
        yield ExecutionEvent(EventType.DONE, {})
        return
    output = "".join(output_parts)
    yield ExecutionEvent(EventType.SKILL_END, {"skill": forced_skill, "status": "passed"})
    data: dict[str, object] = {"text": output}
    if output_was_streamed:
        data["already_streamed"] = True
    yield ExecutionEvent(EventType.TEXT_DELTA, data)
    yield ExecutionEvent(EventType.DONE, {})


# ---------------------------------------------------------------------------
# Runtime preparation
# ---------------------------------------------------------------------------


class _AgentSetup(NamedTuple):
    system_prompt: str
    prompt_manifest: dict[str, object]
    identity: IdentitySpec
    route: RouteDecision
    chain: SkillChain | None
    state_dir: Path
    working_dir: Path
    disabled_skill_names: frozenset[str]
    # Messages carrying (or explaining) images the user referenced by path.
    # Empty for every request that mentions none, which is nearly all of them.
    # A tuple, so the default cannot be a shared mutable list.
    vision_messages: Sequence[dict] = ()


def _merge_context(user_message: str, context: str | None) -> str:
    return f"{user_message}\n\n{context}" if context else user_message


def _missing_skill_message(skill_registry: SkillRegistry, forced_skill: str) -> str:
    available = ", ".join(
        sorted(summary["name"] for summary in skill_registry.list_summaries())
    )
    if not available:
        return f"Skill '{forced_skill}' not found. No skills are currently available."
    return f"Skill '{forced_skill}' not found. Available skills: {available}"


def _enabled_tools_from_config(
    profile_config: dict,
    tool_registry: ToolRegistry,
    identity: IdentitySpec,
) -> list[str]:
    available = tool_registry.list_visible_tool_names(include_disabled=True)
    tools_cfg = profile_config.get("tools") if isinstance(profile_config, dict) else {}
    enabled = tools_cfg.get("enabled") if isinstance(tools_cfg, dict) else None
    if enabled is None:
        # No whitelist configured → default to all non-hidden tools.
        configured = available
    elif isinstance(enabled, list):
        configured = [
            name for name in enabled
            if isinstance(name, str) and name in available
        ]
    else:
        # A malformed whitelist (e.g. `enabled: "shell"`) must fail closed, not
        # silently open every tool — a config typo must never grant shell/file access.
        raise ValueError(
            f"tools.enabled must be a list of tool names, got {type(enabled).__name__}"
        )
    if identity.enabled_tools is None:
        return configured
    allowed = set(identity.enabled_tools)
    return [name for name in configured if name in allowed]


def _runtime_prompt_context(runtime: RuntimeContext, identity: IdentitySpec) -> dict[str, str]:
    context = {
        "agent_id": runtime.agent_id,
        "name": runtime.agent_name,
        "identity_id": identity.id,
        "identity_name": identity.name,
        "_profile_dir": str(runtime.profile_dir),
    }
    if runtime.session_id:
        context["session_id"] = runtime.session_id
    for key, value in runtime.metadata.items():
        context.setdefault(key, value)
    return context


def _runtime_execution_context(
    runtime: RuntimeContext,
    identity: IdentitySpec,
    state_dir: Path,
    working_dir: Path,
) -> dict[str, str | None]:
    context: dict[str, str | None] = {
        CTX_AGENT_ID: runtime.agent_id,
        CTX_SESSION_ID: runtime.session_id,
        CTX_IDENTITY_ID: identity.id,
        CTX_STATE_DIR: str(state_dir),
        CTX_WORKING_DIR: str(working_dir.resolve()),
    }
    for key, value in runtime.metadata.items():
        context.setdefault(key, value)
    return context


def _identity_state_dir(runtime: RuntimeContext, identity: IdentitySpec) -> Path:
    """Return the directory for mutable agent state (memory, checkpoints).

    Single-agent design: state lives directly under profile_dir so that
    the assembler (which reads profile_dir/memory/) and the compilation
    pipeline (which writes here) share the same directory.
    """
    return runtime.profile_dir


async def _load_profile_config(runtime: RuntimeContext) -> dict:
    from common.yaml_utils import load_yaml
    # 文件缺失时 load_yaml 返回 {}（正常默认）；配置损坏必须显式失败——
    # 静默回退空配置会把 tools.enabled 白名单反向放开成全量工具（fail-open）。
    return load_yaml(runtime.profile_dir / "config.yaml")


async def _register_mcp_tools(
    profile_config: dict,
    runtime: RuntimeContext,
    services: RuntimeServices,
) -> None:
    from engine.mcp.config import register_mcp_tools as _mcp_register
    await _mcp_register(profile_config, runtime, services)


async def prepare_runtime(
    request: EngineRequest,
    runtime: RuntimeContext,
    services: RuntimeServices,
) -> _AgentSetup:
    """Prepare prompt, tools, skills, and task routing from an explicit runtime."""
    catalog = runtime.identity_catalog or IdentityCatalog.load(runtime.agents_dir / "identities")
    route = route_task(request.message, catalog, identity_id=request.identity_id)
    identity = route.identity
    state_dir = _identity_state_dir(runtime, identity)
    wd = (
        Path(request.working_dir).expanduser().resolve()
        if request.working_dir
        else Path.cwd().resolve()
    )
    if not wd.is_dir():
        raise ValueError(f"working directory does not exist: {wd}")

    services.tool_registry.load_providers(runtime.agents_dir / "tools")
    services.tool_registry.bind_working_directory(wd)
    if sys.platform == "darwin":
        services.tool_registry.bind_execution_environment(
            MacOSSeatbeltEnvironment(workspace=wd)
        )
    _bind_snapshot_tools(services, runtime.session_id)
    _bind_memory_ops_tool(services, state_dir)
    _bind_skill_manage_tool(services, state_dir)
    _bind_todo_tool(services, state_dir, runtime.session_id)
    profile_config = await _load_profile_config(runtime)
    await _register_mcp_tools(profile_config, runtime, services)

    unknown_tools = services.tool_registry.set_enabled(
        _enabled_tools_from_config(profile_config, services.tool_registry, identity)
    )
    if unknown_tools:
        logger.warning(
            "agent %s configured unknown tools ignored: %s",
            runtime.agent_id, ", ".join(sorted(set(unknown_tools))),
        )

    # 工具全部注册后把定义绑给守卫，metadata-first 安全检查才能生效。
    if services.tool_guard is not None:
        services.tool_guard.set_working_directory(wd)
        services.tool_guard.bind_definitions(services.tool_registry.definitions())

    from common.config import BUILTIN_SKILLS_DIR, PATHS

    PATHS.ensure_base_dirs()
    services.skill_registry.load_builtin(BUILTIN_SKILLS_DIR)
    profile_skills = runtime.profile_dir / "skills"
    if profile_skills.is_dir():
        services.skill_registry.load_agent_skills(profile_skills)
    from engine.skill.settings import disabled_skill_names

    disabled_skills = disabled_skill_names(runtime.profile_dir)
    if disabled_skills:
        services.skill_registry.restrict_to(
            [
                summary["name"]
                for summary in services.skill_registry.list_summaries()
                if summary["name"] not in disabled_skills
            ]
        )
    if identity.enabled_skills is not None:
        services.skill_registry.restrict_to(identity.enabled_skills)
    _bind_skill_load_tool(services)

    from engine.memory.compile import assemble_memory
    from engine.memory.store import retrieve_relevant_memory
    retrieved = await retrieve_relevant_memory(state_dir, request.message)
    memory_text = assemble_memory(
        state_dir / "memory",
        include_durable=False,
    )
    assembler = PromptAssembler()
    runtime_guidance = identity.prompt
    eval_guidance = ""
    if detect_eval_sensitive(request.message):
        eval_guidance = EVAL_SENSITIVE_GUIDANCE

    prompt_assembly = assembler.assemble_detailed(
        runtime.profile_dir, services.tool_registry, services.skill_registry,
        _runtime_prompt_context(runtime, identity),
        retrieved_durable=retrieved.durable,
        retrieved_episodes=retrieved.episodes,
        working_dir=wd,
        memory_text=memory_text,
        runtime_guidance=runtime_guidance,
        eval_guidance=eval_guidance,
        runtime_control=initial_runtime_control_prompt(),
        output_style_path=runtime.agents_dir / "output_style.md",
        max_tokens=prompt_budget_for_llm(services.llm),
    )

    chain = _resolve_pipeline(route, runtime)

    # Only ``request.message`` is scanned. Resolving paths out of model output
    # would let it read any file without passing the tool guard.
    from engine.llm.image_input import resolve_image_messages

    vision_messages = await resolve_image_messages(
        request.message, wd, services.llm, services.vision_llm,
    )

    return _AgentSetup(
        prompt_assembly.text,
        prompt_assembly.manifest.to_trace_data(),
        identity,
        route,
        chain,
        state_dir,
        wd,
        frozenset(disabled_skills),
        vision_messages,
    )


def _bind_memory_ops_tool(services: RuntimeServices, state_dir: Path) -> None:
    memory_dir = state_dir / "memory"
    memory_api = _MemoryToolApi()

    async def episode_runner(memory_dir: Path, topic: str, related: list[dict]):
        from engine.memory.compile import compact_episode
        return await compact_episode(
            memory_dir,
            services.llm,
            topic,
            related,
            reviewer=services.gate_llm,
        )

    def wrapper(func):
        async def execute_with_memory_context(**kwargs):
            kwargs["memory_dir"] = memory_dir
            kwargs["episode_runner"] = episode_runner
            kwargs["memory_api"] = memory_api
            return await func(**kwargs)
        return execute_with_memory_context

    services.tool_registry.wrap_tool("memory_ops", wrapper)


class _MemoryToolApi:
    """Engine-owned memory capability injected into the generic tool provider."""

    def __init__(self) -> None:
        from engine import memory

        self.MANUAL_MEMORY_KINDS = memory.MANUAL_MEMORY_KINDS
        self.MANUAL_EVIDENCE_TYPES = memory.MANUAL_EVIDENCE_TYPES
        self.MEMORY_LAYER_FILES = memory.MEMORY_LAYER_FILES
        self.contains_secret = memory.contains_secret
        self.contains_injection = memory.contains_injection
        self.sanitize_memory_text = memory.sanitize_memory_text
        self.sanitize_event_value = memory.sanitize_event_value
        self.safe_file_in_dir = memory.safe_file_in_dir
        self.safe_markdown_files = memory.safe_markdown_files
        self.atomic_write_text = memory.atomic_write_text

    async def remove_episode_from_index(self, memory_dir: Path, episode_id: str) -> None:
        from engine.memory.search import SearchIndex

        index = SearchIndex(memory_dir / "episodes")
        await index.open()
        try:
            await index.remove_entry(episode_id)
        finally:
            await index.close()


def _bind_snapshot_tools(services: RuntimeServices, session_id: str | None) -> None:
    """Inject session-scoped snapshots into generic write/edit tool content."""
    from engine.snapshot import get_snapshot

    tracker = get_snapshot(session_id or "default").track

    def wrapper(func):
        async def execute_with_snapshot(**kwargs):
            kwargs["_snapshot_tracker"] = tracker
            result = func(**kwargs)
            return await result if inspect.isawaitable(result) else result

        return execute_with_snapshot

    for tool_name in ("write_file", "edit_file"):
        services.tool_registry.wrap_tool(tool_name, wrapper)


def _bind_skill_manage_tool(services: RuntimeServices, state_dir: Path) -> None:
    """Inject profile-local skill storage into the content-layer manager."""
    from engine.skill.store import SkillStore

    skills_dir = state_dir / "skills"
    store = SkillStore(skills_dir)

    def wrapper(func):
        async def execute_with_skill_storage(**kwargs):
            kwargs["agent_skills_dir"] = skills_dir
            kwargs["skill_store"] = store
            result = func(**kwargs)
            return await result if inspect.isawaitable(result) else result

        return execute_with_skill_storage

    services.tool_registry.wrap_tool("skill_manage", wrapper)


def _bind_skill_load_tool(services: RuntimeServices) -> None:
    """Expose only the same per-request registry used for prompt and execution."""
    def load_skill(name: str) -> tuple[str | None, list[str]]:
        skill = services.skill_registry.get(name)
        available = sorted(summary["name"] for summary in services.skill_registry.list_summaries())
        return (skill.content if skill is not None else None, available)

    def wrapper(func):
        async def execute_with_skill_catalog(**kwargs):
            kwargs["skill_loader"] = load_skill
            result = func(**kwargs)
            return await result if inspect.isawaitable(result) else result

        return execute_with_skill_catalog

    services.tool_registry.wrap_tool("skill_load", wrapper)


def _bind_working_directory_tools(services: RuntimeServices, working_dir: Path) -> None:
    """Bind one request workspace without mutating the server process CWD."""
    root = working_dir.resolve()

    def resolve_path(value: object) -> str:
        path = Path(str(value)).expanduser()
        return str(path if path.is_absolute() else root / path)

    def wrapper_for(name: str):
        def wrapper(func):
            async def execute_in_workspace(**kwargs):
                bound = dict(kwargs)
                if name in {
                    "read_file", "write_file", "edit_file", "grep", "glob_files",
                    "list_dir", "read_pdf", "render_pdf_page",
                }:
                    if "path" in bound:
                        bound["path"] = resolve_path(bound["path"])
                    elif name in {"grep", "glob_files", "list_dir"}:
                        bound["path"] = str(root)
                elif name in {"shell", "git_ops"}:
                    bound["cwd"] = resolve_path(bound.get("cwd") or root)
                    if name == "git_ops" and bound.get("path"):
                        bound["path"] = resolve_path(bound["path"])

                if name in {"write_file", "edit_file"}:
                    bound["_work_dir"] = str(root)

                result = func(**bound)
                return await result if inspect.isawaitable(result) else result

            return execute_in_workspace
        return wrapper

    for tool_name in (
        "read_file", "write_file", "edit_file", "grep", "glob_files", "list_dir",
        "read_pdf", "render_pdf_page", "shell", "git_ops",
    ):
        services.tool_registry.wrap_tool(tool_name, wrapper_for(tool_name))


def _bind_todo_tool(
    services: RuntimeServices,
    state_dir: Path,
    session_id: str | None,
) -> None:
    """Persist Todo state per session rather than per imported tool module."""
    token = sha256((session_id or "default").encode("utf-8")).hexdigest()
    todo_file = state_dir / "todos" / f"{token}.json"

    def wrapper(func):
        async def execute_with_session_todos(**kwargs):
            kwargs["todo_file"] = todo_file
            return await func(**kwargs)
        return execute_with_session_todos

    services.tool_registry.wrap_tool("todo", wrapper)


def _resolve_pipeline(
    route: RouteDecision,
    runtime: RuntimeContext,
) -> SkillChain | None:
    """Resolve a YAML pipeline selected by a declarative route decision."""
    if route.pipeline_id is None:
        return None

    # 门禁/条件实现是内容层资产：解析 pipeline YAML 前必须先注册，
    # 否则 from_yaml 的 gate key 查找会对合法内容报 unknown gate。
    gate_content = load_gate_content(runtime.agents_dir)

    # 1. User-defined pipeline in profile
    profile_pipelines = runtime.profile_dir / "pipelines"
    if profile_pipelines.is_dir():
        user_chains = SkillChain.load_pipelines(
            profile_pipelines,
            gate_registry=gate_content.gates,
            condition_registry=gate_content.conditions,
        )
        if route.pipeline_id in user_chains:
            return user_chains[route.pipeline_id]

    # 2. Built-in pipelines from agents/pipelines/
    builtin_pipelines = runtime.agents_dir / "pipelines"
    if builtin_pipelines.is_dir():
        builtin_chains = SkillChain.load_pipelines(
            builtin_pipelines,
            gate_registry=gate_content.gates,
            condition_registry=gate_content.conditions,
        )
        if route.pipeline_id in builtin_chains:
            return builtin_chains[route.pipeline_id]

    raise RuntimeError(
        f"Route {route.identity_id}:{route.route_id} references missing pipeline {route.pipeline_id!r}"
    )


# ---------------------------------------------------------------------------
# Lifecycle: persistence + cleanup + terminal state
# ---------------------------------------------------------------------------


def _ensure_memory_lifecycle_hooks(services: RuntimeServices) -> None:
    maintenance_llm = services.background_llm or services.llm
    if services.hooks is None:
        from engine.hook import HookManager

        services.hooks = HookManager()
    hook_key = (
        id(maintenance_llm),
        id(services.gate_llm),
        services.owns_llm_clients,
        id(services.hooks),
    )
    if (
        services._memory_lifecycle_hook is not None
        and services._memory_lifecycle_hook_key == hook_key
        and services.hooks.is_registered(services._memory_lifecycle_hook)
    ):
        return
    from engine.execution.memory.memory_maintenance import (
        MemoryLifecycleHooks,
        MemoryMaintenanceService,
    )
    if services._memory_lifecycle_hook is not None:
        services.hooks.unregister(services._memory_lifecycle_hook)

    hook = MemoryLifecycleHooks(
        MemoryMaintenanceService(
            maintenance_llm,
            reviewer=services.gate_llm,
            defer_maintenance=not services.owns_llm_clients,
        )
    )
    services.hooks.register(hook)
    services._memory_lifecycle_hook = hook
    services._memory_lifecycle_hook_key = hook_key


async def run_memory_idle_tick(memory_dir: Path, services: RuntimeServices) -> bool:
    """Dispatch idle memory maintenance through lifecycle hooks."""
    return await _dispatch_memory_maintenance_tick(
        "memory_idle_tick",
        memory_dir,
        services,
    )


async def run_memory_daily_tick(memory_dir: Path, services: RuntimeServices) -> bool:
    """Dispatch daily memory maintenance through lifecycle hooks."""
    return await _dispatch_memory_maintenance_tick(
        "memory_daily_tick",
        memory_dir,
        services,
    )


async def _dispatch_memory_maintenance_tick(
    hook_name: str,
    memory_dir: Path,
    services: RuntimeServices,
) -> bool:
    _ensure_memory_lifecycle_hooks(services)
    try:
        from engine.hook import HookType

        results = await services.hooks.apply(
            hook_name,
            HookType.PARALLEL,
            args=(memory_dir,),
            include_failures=True,
        )
        return all(result is not False for result in results)
    except Exception:
        logger.warning("failed to dispatch %s", hook_name, exc_info=True)
        return False


async def _persist_runtime_learning(
    state_dir: Path,
    user_message: str,
    reply_text: str,
    had_tools: bool,
    services: RuntimeServices,
    *,
    terminal_status: str = "completed",
    terminal_reason: str | None = None,
) -> bool:
    """Persist memory and preferences. Returns False if any write failed."""
    ok = True
    learning_signals: list[str] = []
    learner = None
    try:
        from engine.memory.user_learner import UserPreferenceLearner
        learner = UserPreferenceLearner(state_dir)
        learning_signals = await learner.observe(user_message, reply_text)
    except Exception:
        ok = False
        logger.warning("failed to extract user-preference signals", exc_info=True)

    _ensure_memory_lifecycle_hooks(services)
    try:
        from engine.hook import HookType

        hook_name = {
            "completed": "memory_after_turn_completed",
            "incomplete": "memory_after_turn_incomplete",
            "failed": "memory_after_turn_failed",
        }.get(terminal_status, "memory_after_turn_failed")
        hook_args = (state_dir, user_message, reply_text, had_tools, learning_signals)
        if terminal_status != "completed":
            hook_args += (terminal_reason,)
        results = await services.hooks.apply(
            hook_name,
            HookType.PARALLEL,
            args=hook_args,
            include_failures=True,
        )
        ok = ok and all(result is not False for result in results)
    except Exception:
        ok = False
        logger.warning("failed to persist conversation memory", exc_info=True)
    if ok and learner is not None and learning_signals:
        try:
            learner.acknowledge(learning_signals)
        except Exception:
            ok = False
            logger.warning("failed to acknowledge user-preference signals", exc_info=True)
    return ok


def _has_successful_tool_evidence(event: ExecutionEvent) -> bool:
    """Return whether an event carries real, successful tool evidence.

    Tool starts only describe a model proposal.  Preflight challenges, policy
    blocks, and provider/tool failures never produced project evidence and
    must not make the memory pipeline label the turn as ``tool_result``.
    """
    return (
        event.type is EventType.TOOL_CALL_RESULT
        and not bool(event.data.get("blocked"))
        and not bool(event.data.get("preflight"))
        and not bool(event.data.get("error"))
    )


def _fact_gate_for_request(
    request: EngineRequest,
    runtime: RuntimeContext,
    services: RuntimeServices | None = None,
) -> FactGate:
    definitions = services.tool_registry.definitions() if services is not None else None
    return FactGate(FactGateContext(
        session_id=runtime.session_id or "",
        turn_id=uuid4().hex,
    ), tool_registry=definitions)


def _record_observability_event(
    observation: RunObservation | None,
    event: ExecutionEvent,
) -> None:
    """Send an execution event through the single observability boundary."""
    if observation is not None:
        observation.record(event)


def _start_run_observation(
    runtime: RuntimeContext,
    request: EngineRequest,
    run_id: str,
    state_store: RunStateStore | None,
) -> RunObservation:
    """Create the local observability boundary for one execution attempt."""
    context = RunObservationContext(
        run_id=run_id,
        agent_id=runtime.agent_id,
        session_id=runtime.session_id,
        identity_id=request.identity_id,
        working_dir=request.working_dir,
        forced_skill=request.forced_skill,
        profile_dir=runtime.profile_dir,
    )
    return RunObservation.start(
        context,
        projections=(lambda event: project_execution_event(state_store, run_id, event),),
    )


def run_stream_with_runtime(
    request: EngineRequest,
    runtime: RuntimeContext,
    services: RuntimeServices,
) -> AgentRunStream:
    """Create a typed, single-consumer stream for one Agent run."""
    run_id = uuid4().hex
    state_store: RunStateStore | None = None
    try:
        state_store = RunStateStore(runtime.profile_dir)
        state_store.create(
            run_id,
            agent_id=runtime.agent_id,
            session_id=runtime.session_id,
            message_id=request.message_id,
            identity_id=request.identity_id,
            working_dir=request.working_dir,
            forced_skill=request.forced_skill,
        )
    except (RunStateError, OSError, ValueError):
        logger.warning("failed to initialize run state (run=%s)", run_id, exc_info=True)
        state_store = None
    observation = _start_run_observation(runtime, request, run_id, state_store)
    try:
        ledger: ToolExecutionLedger | None = ToolExecutionLedger(runtime.profile_dir, run_id)
    except Exception:
        logger.warning("failed to initialize tool execution ledger (run=%s)", run_id, exc_info=True)
        if state_store is not None:
            try:
                state_store.transition(
                    run_id,
                    RunStatus.FAILED,
                    event_type="run_setup_failed",
                    reason="tool_ledger_unavailable",
                    error="tool_ledger_unavailable",
                )
            except (RunStateError, OSError, ValueError):
                logger.warning(
                    "failed to mark tool ledger setup failure (run=%s)",
                    run_id,
                    exc_info=True,
                )
        return _failed_setup_stream(run_id, services, "tool_ledger_unavailable", observation)
    return AgentRunStream(
        run_id,
        _run_events_with_runtime(
            request,
            runtime,
            services,
            run_id,
            state_store,
            observation,
            ledger,
        ),
        on_unstarted_close=lambda: _cancel_unstarted_run(run_id, state_store, observation, services),
    )


async def _cancel_unstarted_run(
    run_id: str,
    state_store: RunStateStore | None,
    observation: RunObservation | None,
    services: RuntimeServices,
) -> None:
    """Clean up a run whose consumer closes the stream before its first event."""
    cancelled_event = ExecutionEvent(EventType.RUN_FINISHED, {
        "run_id": run_id,
        "status": RunStatus.CANCELLED.value,
        "reason": "consumer_disconnected",
    })
    if observation is not None:
        _record_observability_event(observation, cancelled_event)
    elif state_store is not None:
        try:
            state_store.transition(
                run_id,
                RunStatus.CANCELLED,
                event_type="run_cancelled",
                reason="consumer_disconnected",
            )
        except (RunStateError, OSError, ValueError):
            logger.warning("failed to mark cancelled run (run=%s)", run_id, exc_info=True)
    try:
        await services.close()
    except Exception:
        logger.warning("failed to close engine runtime services", exc_info=True)
    APPROVAL_BROKER.cancel_run(run_id)


def _failed_setup_stream(
    run_id: str,
    services: RuntimeServices,
    reason: str,
    observation: RunObservation | None = None,
) -> AgentRunStream:
    """Expose setup failures through the same terminal stream contract."""

    async def close_unstarted() -> None:
        try:
            await services.close()
        except Exception:
            logger.warning("failed to close services after setup failure", exc_info=True)

    async def events() -> AsyncGenerator[ExecutionEvent, None]:
        try:
            for event in (
                ExecutionEvent(EventType.RUN_STARTED, {"run_id": run_id}),
                ExecutionEvent(EventType.FAILED, {"reason": reason}),
                ExecutionEvent(EventType.DONE, {}),
                ExecutionEvent(
                    EventType.RUN_FINISHED,
                    {"run_id": run_id, "status": "failed", "reason": reason},
                ),
            ):
                _record_observability_event(observation, event)
                yield event
        finally:
            try:
                await services.close()
            except Exception:
                logger.warning("failed to close services after setup failure", exc_info=True)

    return AgentRunStream(run_id, events(), on_unstarted_close=close_unstarted)


def resume_stream_with_runtime(
    request: EngineRequest,
    runtime: RuntimeContext,
    services: RuntimeServices,
    run_id: str,
) -> AgentRunStream:
    """Resume a recoverable run using its persisted state and tool ledger.

    The caller must provide the same session history in ``request.history``.
    Completed side-effecting calls are replayed by the run's ledger; calls
    whose prior side effect is uncertain remain blocked until an operator
    resolves them.
    """
    try:
        state_store = RunStateStore(runtime.profile_dir)
        ledger = ToolExecutionLedger(runtime.profile_dir, run_id, replay_existing=True)
        state_store.resume(run_id)
    except Exception:
        logger.warning("failed to resume run (run=%s)", run_id, exc_info=True)
        return _failed_setup_stream(
            run_id,
            services,
            "resume_setup_failed",
            _start_run_observation(runtime, request, run_id, None),
        )
    observation = _start_run_observation(runtime, request, run_id, state_store)
    return AgentRunStream(
        run_id,
        _run_events_with_runtime(
            request,
            runtime,
            services,
            run_id,
            state_store,
            observation,
            ledger,
        ),
    )


async def _run_events_with_runtime(
    request: EngineRequest,
    runtime: RuntimeContext,
    services: RuntimeServices,
    run_id: str,
    state_store: RunStateStore | None = None,
    observation: RunObservation | None = None,
    ledger: ToolExecutionLedger | None = None,
) -> AsyncGenerator[ExecutionEvent, None]:
    """Produce one complete run, including persistence and cleanup."""
    full_text: list[str] = []
    had_tools = False
    terminal_status = "completed"
    terminal_reason: str | None = None
    drained = False
    state_dir: Path | None = None
    memory_persist_failed = False

    if ledger is not None:
        services.tool_registry.bind_execution_ledger(ledger)

    try:
        run_started = ExecutionEvent(
            EventType.RUN_STARTED,
            {
                "run_id": run_id,
                "project_path": request.working_dir or "",
            },
        )
        _record_observability_event(observation, run_started)
        yield run_started
        s = await prepare_runtime(request, runtime, services)
        state_dir = s.state_dir
        if observation is not None and hasattr(s, "prompt_manifest"):
            observation.append_prompt_manifest(s.prompt_manifest)
        guard = FailureLoopGuard()
        with use_fact_gate(_fact_gate_for_request(request, runtime, services)), use_approval_context(
            APPROVAL_BROKER, run_id
        ), generation_context(run_id=run_id, session_id=runtime.session_id), llm_purpose("main"):
            async for event in run_agent_stream(
                services.llm, s.system_prompt,
                _merge_context(request.message, request.context),
                services.tool_registry, services.skill_registry,
                s.route, s.chain, guard,
                tool_guard=services.tool_guard,
                history=request.history,
                forced_skill=request.forced_skill,
                execution_context=_runtime_execution_context(
                    runtime, s.identity, s.state_dir, s.working_dir,
                ),
                gate_llm=services.gate_llm,
                disabled_skill_names=getattr(s, "disabled_skill_names", frozenset()),
                vision_messages=getattr(s, "vision_messages", ()),
            ):
                if event.type == EventType.TEXT_DELTA:
                    full_text.append(str(event.data.get("text", "")))
                elif event.type == EventType.INCOMPLETE:
                    terminal_status = "incomplete"
                    terminal_reason = str(event.data.get("reason", "agent_incomplete"))
                elif event.type == EventType.FAILED:
                    terminal_status = "failed"
                    terminal_reason = str(event.data.get("reason", "agent_failed"))
                elif event.type == EventType.BLOCKED and terminal_status == "completed":
                    terminal_status = "incomplete"
                    terminal_reason = "blocked"
                elif _has_successful_tool_evidence(event):
                    had_tools = True
                _record_observability_event(observation, event)
                yield event
        drained = True
    except Exception as exc:
        logger.exception("agent execution failed (agent=%s)", runtime.agent_id)
        terminal_status = "failed"
        terminal_reason = "execution_error"
        failure_text = ExecutionEvent(EventType.TEXT_DELTA, {
            "text": f"⚠️ 执行失败：{type(exc).__name__}（详情见服务端日志）",
        })
        _record_observability_event(observation, failure_text)
        yield failure_text
        failure_event = ExecutionEvent(EventType.FAILED, {"reason": terminal_reason})
        _record_observability_event(observation, failure_event)
        yield failure_event
        done_event = ExecutionEvent(EventType.DONE, {})
        _record_observability_event(observation, done_event)
        yield done_event
        drained = True
    finally:
        if (
            drained
            and state_dir is not None
            and terminal_status in {"completed", "incomplete", "failed"}
        ):
            try:
                memory_persist_failed = not await asyncio.wait_for(
                    _persist_runtime_learning(
                        state_dir, request.message, "".join(full_text), had_tools, services,
                        terminal_status=terminal_status,
                        terminal_reason=terminal_reason,
                    ),
                    timeout=_RUNTIME_LEARNING_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                memory_persist_failed = True
                logger.warning(
                    "runtime learning finalization timed out after %.1fs (run=%s)",
                    _RUNTIME_LEARNING_TIMEOUT_SECONDS,
                    run_id,
                )
            except Exception:
                memory_persist_failed = True
                logger.warning("failed to finalize conversation memory", exc_info=True)
        if not drained:
            cancelled_event = ExecutionEvent(EventType.RUN_FINISHED, {
                "run_id": run_id,
                "status": RunStatus.CANCELLED.value,
                "reason": "consumer_disconnected",
            })
            if observation is not None:
                _record_observability_event(observation, cancelled_event)
            elif state_store is not None:
                try:
                    state_store.transition(
                        run_id,
                        RunStatus.CANCELLED,
                        event_type="run_cancelled",
                        reason="consumer_disconnected",
                    )
                except (RunStateError, OSError, ValueError):
                    logger.warning("failed to mark cancelled run (run=%s)", run_id, exc_info=True)
        try:
            await services.close()
        except Exception:
            logger.warning("failed to close engine runtime services", exc_info=True)
        if ledger is not None:
            services.tool_registry.bind_execution_ledger(None)
        APPROVAL_BROKER.cancel_run(run_id)

    if drained:
        terminal_data: dict[str, object] = {"run_id": run_id, "status": terminal_status}
        if terminal_reason:
            terminal_data["reason"] = terminal_reason
        if memory_persist_failed:
            # 记忆写入失败对用户默认不可见；在终态事件上打标，
            # 让前端有机会提示"本轮未写入长期记忆"。
            terminal_data["memory_persist_failed"] = True
        finished_event = ExecutionEvent(EventType.RUN_FINISHED, terminal_data)
        _record_observability_event(observation, finished_event)
        yield finished_event


# ---------------------------------------------------------------------------
# Entry points (non-streaming + compatibility)
# ---------------------------------------------------------------------------


async def reply_with_runtime(
    request: EngineRequest,
    runtime: RuntimeContext,
    services: RuntimeServices,
) -> EngineResult:
    """Run one engine request using the same complete stream lifecycle as SSE."""
    full_text: list[str] = []
    had_tools = False

    stream = run_stream_with_runtime(request, runtime, services)
    events = stream.stream_events()
    try:
        async for event in events:
            if event.type == EventType.TEXT_DELTA:
                full_text.append(str(event.data.get("text", "")))
            elif _has_successful_tool_evidence(event):
                had_tools = True
    finally:
        await events.aclose()

    if not stream.is_complete:
        raise RuntimeError("Agent run ended before a terminal state was emitted.")
    if stream.status == "failed":
        raise RuntimeError(stream.reason or "agent_failed")
    if stream.status == "incomplete":
        raise IncompleteAgentRunError(stream.reason or "agent_incomplete")

    return EngineResult(text="".join(full_text), had_tools=had_tools)


async def reply_events_with_runtime(
    request: EngineRequest,
    runtime: RuntimeContext,
    services: RuntimeServices,
) -> AsyncGenerator[ExecutionEvent, None]:
    """Compatibility adapter over run_stream_with_runtime."""
    stream = run_stream_with_runtime(request, runtime, services)
    events = stream.stream_events()
    try:
        async for event in events:
            yield event
    finally:
        await events.aclose()


async def reply_stream_with_runtime(
    request: EngineRequest,
    runtime: RuntimeContext,
    services: RuntimeServices,
) -> AsyncGenerator[str, None]:
    """Text-only stream adapter."""
    saw_raw_text = False
    async for event in reply_events_with_runtime(request, runtime, services):
        text = raw_text_delta(event, include_provisional=False)
        if text is not None:
            saw_raw_text = True
            yield text
        elif event.type == EventType.TEXT_DELTA:
            if not event.data.get("already_streamed") or not saw_raw_text:
                yield event.data.get("text", "")
        elif event.type == EventType.SKILL_START:
            yield f"\n[⚙ {event.data.get('skill', '')}]\n"
        elif event.type == EventType.GATE_RESULT:
            yield f"[门禁: {event.data.get('verdict', '')}] "
        elif event.type == EventType.BACKTRACK:
            yield f"\n[↩ 回退: {event.data.get('from', '')} → {event.data.get('to', '')}]\n"
        elif event.type == EventType.BLOCKED:
            yield f"\n[⛔ 阻断: {event.data.get('reason', '')}]\n"
