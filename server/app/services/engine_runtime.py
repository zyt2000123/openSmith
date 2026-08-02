from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common import config as common_config
from engine.execution import RuntimeContext, RuntimeServices, validate_execution_assets
from engine.identity import IdentityCatalog, load_identity_catalog
from engine.llm.model_config import LLMUsage, build_llm_client, resolve_llm_config
from engine.llm.contracts import GEMINI_OPENAI_BASE_URL
from engine.llm.factory import normalize_provider_name
from engine.llm.port import LLMPort
from engine.observability import RunObservation
from engine.safety.tool_guard import ToolGuard
from engine.skill.registry import SkillRegistry
from engine.tool.registry import ToolRegistry
from engine.mcp.session_pool import MCPClientSessionPool


def _config_fingerprint(config: dict[str, Any]) -> str:
    """Stable cache key for a fully resolved LLM route."""
    return json.dumps(_normalize_llm_config(config), sort_keys=True, separators=(",", ":"), default=str)


def _normalize_llm_config(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize equivalent LLM configs before cache lookup."""
    normalized = dict(config)
    # Display metadata must never affect client reuse or provider requests.
    normalized.pop("vendor", None)
    provider = normalize_provider_name(normalized.get("provider", ""))
    normalized["provider"] = provider
    if provider == "gemini" and not str(normalized.get("base_url") or "").strip():
        normalized["base_url"] = GEMINI_OPENAI_BASE_URL
    return normalized


def _maybe_record(client: LLMPort) -> LLMPort:
    """Wrap the client so real runs land in a JSONL recording (opt-in).

    Set ``AGENT_SMITH_RECORD_LLM=/path/to/case.jsonl`` and every model turn of
    every subsequent run appends there, ready to replay via
    :mod:`engine.llm.replay`.
    Only the *responses* are written, never the prompt — so a recording cannot
    leak conversation content, and replay does not need it (turns are served in
    recorded order rather than matched against messages).
    """
    target = os.environ.get("AGENT_SMITH_RECORD_LLM", "").strip()
    if not target:
        return client
    from engine.llm.replay import RecordingLLM

    path = Path(target).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return RecordingLLM(client, path)


@dataclass
class LLMClientManager:
    """Factory/cache for process-scoped LLM clients."""

    _clients: dict[str, LLMPort] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def get(self, usage: LLMUsage) -> LLMPort:
        config = resolve_llm_config(usage=usage)
        return self.get_for_config(config)

    def get_for_config(self, config: dict[str, Any]) -> LLMPort:
        fingerprint = _config_fingerprint(config)
        with self._lock:
            client = self._clients.get(fingerprint)
            if client is None:
                client = _maybe_record(build_llm_client(config))
                self._clients[fingerprint] = client
            return client

    async def close(self) -> None:
        with self._lock:
            clients = list({id(client): client for client in self._clients.values()}.values())
            self._clients.clear()
        for client in clients:
            await client.close()


_llm_client_manager = LLMClientManager()
_mcp_client_session_pool = MCPClientSessionPool()


def _single_line_runtime_value(value: object) -> str:
    """Return a small display-safe runtime fact without widening config exposure."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:200]


def _interactive_model_metadata(config: dict[str, Any]) -> dict[str, str]:
    """Expose only the active chat route's non-secret identity to the engine."""
    metadata: dict[str, str] = {}
    vendor = _single_line_runtime_value(config.get("vendor"))
    provider = _single_line_runtime_value(config.get("provider"))
    model = _single_line_runtime_value(config.get("model"))
    if vendor:
        metadata["current_vendor"] = vendor
    if provider:
        metadata["current_provider"] = normalize_provider_name(provider)
    if model:
        metadata["current_model"] = model
    return metadata


def load_runtime_identity_catalog(*, force: bool = False) -> IdentityCatalog:
    """Load the one catalog and validate its declared assets for every entry point."""
    paths = common_config.PATHS
    catalog = load_identity_catalog(paths.builtin_identities_dir, force=force)
    skill_registry = SkillRegistry()
    tool_registry = ToolRegistry()
    paths.ensure_base_dirs()
    skill_registry.load_builtin(paths.builtin_skills_dir)
    skill_registry.load_agent_skills(paths.agent_dir / "skills")
    tool_registry.load_builtin_providers(paths.project_root / "agents" / "tools")
    validate_execution_assets(
        catalog,
        agents_dir=paths.project_root / "agents",
        skill_names=(summary["name"] for summary in skill_registry.list_summaries()),
        tool_names=tool_registry.list_tool_names(),
    )
    return catalog


def build_engine_runtime(
    agent_id: str,
    agent_name: str,
    *,
    session_id: str | None = None,
    model_profile: str | None = None,
    llm_client_manager: LLMClientManager | None = None,
) -> tuple[RuntimeContext, RuntimeServices]:
    """Build the engine runtime for the FastAPI product layer."""
    manager = llm_client_manager or _llm_client_manager
    paths = common_config.PATHS
    interactive_kwargs: dict[str, Any] = {"usage": LLMUsage.INTERACTIVE}
    if model_profile:
        interactive_kwargs["model_profile"] = model_profile
    interactive_config = resolve_llm_config(**interactive_kwargs)
    gate_config = resolve_llm_config(usage=LLMUsage.GATE)
    background_config = resolve_llm_config(usage=LLMUsage.BACKGROUND)
    runtime = RuntimeContext(
        agent_id=agent_id,
        agent_name=agent_name,
        profile_dir=paths.agent_dir,
        agents_dir=paths.project_root / "agents",
        session_id=session_id,
        metadata=_interactive_model_metadata(interactive_config),
        identity_catalog=load_runtime_identity_catalog(),
    )
    skill_registry = SkillRegistry()
    skill_registry.load_builtin(paths.builtin_skills_dir)
    services = RuntimeServices(
        llm=manager.get_for_config(interactive_config),
        gate_llm=manager.get_for_config(gate_config),
        background_llm=manager.get_for_config(background_config),
        tool_registry=ToolRegistry(),
        skill_registry=skill_registry,
        tool_guard=ToolGuard(paths.safety_rules_path),
        mcp_session_pool=_mcp_client_session_pool,
        owns_mcp_clients=False,
        observation_factory=RunObservation.start,
        owns_llm_clients=False,
    )
    return runtime, services


def build_memory_maintenance_services() -> RuntimeServices:
    """Build scheduler-safe services backed by process-scoped LLM clients."""
    gate_config = resolve_llm_config(usage=LLMUsage.GATE)
    background_config = resolve_llm_config(usage=LLMUsage.BACKGROUND)
    background_llm = _llm_client_manager.get_for_config(background_config)
    return RuntimeServices(
        llm=background_llm,
        gate_llm=_llm_client_manager.get_for_config(gate_config),
        background_llm=background_llm,
        tool_registry=ToolRegistry(),
        skill_registry=SkillRegistry(),
        owns_llm_clients=False,
    )


async def close_shared_llm_clients() -> None:
    """Close process-scoped MCP and LLM clients during server shutdown."""
    await _mcp_client_session_pool.close()
    await _llm_client_manager.close()


async def close_session_mcp_clients(session_id: str) -> None:
    """Release MCP resources when their owning conversation is deleted."""
    await _mcp_client_session_pool.release(session_id)
