"""Prepare one Engine run without owning its execution lifecycle."""

from __future__ import annotations

import hashlib
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

from engine.context import PromptAssembler, estimate_tokens, prompt_budget_for_llm
from engine.execution.pipeline.pipeline_context import (
    CTX_AGENT_ID,
    CTX_IDENTITY_ID,
    CTX_RUN_ID,
    CTX_SESSION_ID,
    CTX_STATE_DIR,
    CTX_WORKING_DIR,
)
from engine.execution.pipeline.skill_chain import SkillChain, load_gate_content
from engine.execution.routing.task_router import route_task
from engine.execution.runtime_control import initial_runtime_control_prompt
from engine.identity import IdentityCatalog, IdentitySpec, RouteDecision
from engine.safety.eval_guard import EVAL_SENSITIVE_GUIDANCE, detect_eval_sensitive
from engine.sandbox import MacOSSeatbeltEnvironment
from engine.tool.registry import ToolRegistry

from .builtin_tools import (
    bind_memory_ops_tool,
    bind_skill_load_tool,
    bind_skill_manage_tool,
    bind_snapshot_tools,
    bind_todo_tool,
)
from .runtime import EngineRequest, RuntimeContext, RuntimeServices

logger = logging.getLogger(__name__)


class AgentSetup(NamedTuple):
    """Prepared, immutable inputs consumed by the run lifecycle."""

    system_prompt: str
    prompt_manifest: dict[str, object]
    prefix_cache_key: str
    identity: IdentitySpec
    route: RouteDecision
    chain: SkillChain | None
    state_dir: Path
    working_dir: Path
    disabled_skill_names: frozenset[str]


def merge_request_context(user_message: str, context: str | None) -> str:
    return f"{user_message}\n\n{context}" if context else user_message


def enabled_tools_from_config(
    profile_config: dict,
    tool_registry: ToolRegistry,
    identity: IdentitySpec,
) -> list[str]:
    available = tool_registry.list_visible_tool_names(include_disabled=True)
    tools_cfg = profile_config.get("tools") if isinstance(profile_config, dict) else {}
    enabled = tools_cfg.get("enabled") if isinstance(tools_cfg, dict) else None
    if enabled is None:
        configured = available
    elif isinstance(enabled, list):
        configured = [
            name
            for name in enabled
            if isinstance(name, str) and name in available
        ]
    else:
        raise ValueError(
            f"tools.enabled must be a list of tool names, got {type(enabled).__name__}"
        )
    if identity.enabled_tools is None:
        return configured
    allowed = set(identity.enabled_tools)
    return [name for name in configured if name in allowed]


def _runtime_prompt_context(
    runtime: RuntimeContext,
    identity: IdentitySpec,
) -> dict[str, str]:
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


def runtime_execution_context(
    runtime: RuntimeContext,
    identity: IdentitySpec,
    state_dir: Path,
    working_dir: Path,
    run_id: str | None = None,
) -> dict[str, str | None]:
    context: dict[str, str | None] = {
        CTX_AGENT_ID: runtime.agent_id,
        CTX_SESSION_ID: runtime.session_id,
        CTX_IDENTITY_ID: identity.id,
        CTX_STATE_DIR: str(state_dir),
        CTX_WORKING_DIR: str(working_dir.resolve()),
        CTX_RUN_ID: run_id or "",
    }
    for key, value in runtime.metadata.items():
        context.setdefault(key, value)
    return context


def _identity_state_dir(runtime: RuntimeContext) -> Path:
    """Return the shared directory for mutable agent state."""
    return runtime.profile_dir


async def _load_profile_config(runtime: RuntimeContext) -> dict:
    from common.yaml_utils import load_yaml

    # Missing config is a normal default. Invalid config must fail explicitly:
    # an empty fallback would turn a malformed allowlist into fail-open access.
    return load_yaml(runtime.profile_dir / "config.yaml")


async def _register_mcp_tools(
    profile_config: dict,
    runtime: RuntimeContext,
    services: RuntimeServices,
) -> None:
    from engine.mcp.config import register_configured_mcp_tools

    registration = await register_configured_mcp_tools(
        profile_config,
        session_id=runtime.session_id,
        agent_id=runtime.agent_id,
        tool_registry=services.tool_registry,
        session_pool=services.mcp_session_pool,
    )
    services.mcp_clients.extend(registration.clients)


async def prepare_runtime(
    request: EngineRequest,
    runtime: RuntimeContext,
    services: RuntimeServices,
) -> AgentSetup:
    """Resolve routing, tools, memory, prompt, and model input for one run."""
    catalog = runtime.identity_catalog or IdentityCatalog.load(
        runtime.agents_dir / "identities"
    )
    route = route_task(request.message, catalog, identity_id=request.identity_id)
    identity = route.identity
    state_dir = _identity_state_dir(runtime)
    working_dir = (
        Path(request.working_dir).expanduser().resolve()
        if request.working_dir
        else Path.cwd().resolve()
    )
    if not working_dir.is_dir():
        raise ValueError(f"working directory does not exist: {working_dir}")

    services.tool_registry.load_providers(runtime.agents_dir / "tools")
    services.tool_registry.bind_working_directory(working_dir)
    if sys.platform == "darwin":
        services.tool_registry.bind_execution_environment(
            MacOSSeatbeltEnvironment(workspace=working_dir)
        )
    bind_snapshot_tools(services, runtime.session_id)
    bind_memory_ops_tool(services, state_dir)
    bind_todo_tool(services, state_dir, runtime.session_id)
    profile_config = await _load_profile_config(runtime)
    await _register_mcp_tools(profile_config, runtime, services)

    unknown_tools = services.tool_registry.set_enabled(
        enabled_tools_from_config(profile_config, services.tool_registry, identity)
    )
    if unknown_tools:
        logger.warning(
            "agent %s configured unknown tools ignored: %s",
            runtime.agent_id,
            ", ".join(sorted(set(unknown_tools))),
        )

    if services.tool_guard is not None:
        services.tool_guard.set_working_directory(working_dir)
        services.tool_guard.bind_definitions(services.tool_registry.definitions())
    services.tool_registry.bind_tool_guard(services.tool_guard)

    profile_skills = runtime.profile_dir / "skills"
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
    bind_skill_manage_tool(
        services,
        state_dir,
        disabled_skills=frozenset(disabled_skills),
        enabled_skills=identity.enabled_skills,
    )
    bind_skill_load_tool(services)

    from engine.memory.compile import assemble_memory, ensure_durable_template
    from engine.memory.store import retrieve_relevant_memory

    try:
        ensure_durable_template(state_dir / "memory")
    except Exception:
        logger.warning("failed to initialize durable memory template", exc_info=True)
    retrieved = await retrieve_relevant_memory(state_dir, request.message)
    memory_text = assemble_memory(state_dir / "memory", include_durable=False)
    eval_guidance = (
        EVAL_SENSITIVE_GUIDANCE if detect_eval_sensitive(request.message) else ""
    )
    prompt_assembly = PromptAssembler().assemble_detailed(
        runtime.profile_dir,
        services.tool_registry,
        services.skill_registry,
        _runtime_prompt_context(runtime, identity),
        retrieved_durable=retrieved.durable,
        retrieved_episodes=retrieved.episodes,
        working_dir=working_dir,
        memory_text=memory_text,
        runtime_guidance=identity.prompt,
        eval_guidance=eval_guidance,
        runtime_control=initial_runtime_control_prompt(),
        output_style_path=runtime.agents_dir / "output_style.md",
        max_tokens=prompt_budget_for_llm(services.llm),
    )
    if services.hooks is not None:
        from engine.execution.hooks import HookType

        hooked_prompt = await services.hooks.apply(
            "system_prompt",
            HookType.SERIES_LAST,
            initial=prompt_assembly.text,
        )
        if (
            isinstance(hooked_prompt, str)
            and hooked_prompt.strip()
            and hooked_prompt != prompt_assembly.text
        ):
            rendered_tokens = estimate_tokens(hooked_prompt)
            plan = replace(
                prompt_assembly.plan,
                rendered_tokens=rendered_tokens,
                within_budget=(
                    not prompt_assembly.plan.token_budget
                    or rendered_tokens <= prompt_assembly.plan.token_budget
                ),
            )
            hook_entry = {
                "id": "system_prompt_hooks",
                "source": "runtime",
                "source_ref": "hooks:system_prompt",
                "scope": "runtime",
                "authority": "agent_policy",
                "trust": "configured",
                "load_reason": "runtime_only",
                "content_hash": hashlib.sha256(
                    hooked_prompt.encode()
                ).hexdigest(),
                "char_count": len(hooked_prompt),
                "token_estimate": rendered_tokens,
                "action": "modified",
            }
            manifest = replace(
                prompt_assembly.manifest,
                rendered_prompt_hash=hashlib.sha256(
                    hooked_prompt.encode()
                ).hexdigest(),
                layers=(*prompt_assembly.manifest.layers, hook_entry),
                budget=plan.to_trace_data(),
            )
            prefix_cache_key = hashlib.sha256(
                (
                    prompt_assembly.prefix_cache_key
                    + ":"
                    + manifest.rendered_prompt_hash
                ).encode()
            ).hexdigest()
            prompt_assembly = replace(
                prompt_assembly,
                text=hooked_prompt,
                manifest=manifest,
                prefix_cache_key=prefix_cache_key,
                plan=plan,
            )
        elif hooked_prompt is not None and (
            not isinstance(hooked_prompt, str) or not hooked_prompt.strip()
        ):
            logger.warning(
                "system_prompt hook returned an invalid value; keeping assembled prompt"
            )

    chain = _resolve_pipeline(route, runtime)

    return AgentSetup(
        system_prompt=prompt_assembly.text,
        prompt_manifest=prompt_assembly.manifest.to_trace_data(),
        prefix_cache_key=prompt_assembly.prefix_cache_key,
        identity=identity,
        route=route,
        chain=chain,
        state_dir=state_dir,
        working_dir=working_dir,
        disabled_skill_names=frozenset(disabled_skills),
    )


def _resolve_pipeline(
    route: RouteDecision,
    runtime: RuntimeContext,
) -> SkillChain | None:
    """Resolve a YAML pipeline selected by a declarative route decision."""
    if route.pipeline_id is None:
        return None

    gate_content = load_gate_content(runtime.agents_dir)

    profile_pipelines = runtime.profile_dir / "pipelines"
    if profile_pipelines.is_dir():
        user_chains = SkillChain.load_pipelines(
            profile_pipelines,
            gate_registry=gate_content.gates,
            condition_registry=gate_content.conditions,
        )
        if route.pipeline_id in user_chains:
            return user_chains[route.pipeline_id]

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
        f"Route {route.identity_id}:{route.route_id} references missing pipeline "
        f"{route.pipeline_id!r}"
    )


__all__ = (
    "AgentSetup",
    "enabled_tools_from_config",
    "merge_request_context",
    "prepare_runtime",
    "runtime_execution_context",
)
