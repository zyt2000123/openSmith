from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from engine.execution.events import RunObservationFactory
from engine.identity import IdentityCatalog
from engine.llm.port import LLMPort
from engine.safety.tool_guard import ToolGuard
from engine.skill.registry import SkillRegistry
from engine.tool.registry import ToolRegistry

if TYPE_CHECKING:
    from engine.execution.hooks import HookManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EngineRequest:
    """A single user request submitted to the engine."""

    message: str
    history: list[dict] | None = None
    context: str | None = None
    forced_skill: str | None = None
    identity_id: str | None = None
    working_dir: str | None = None
    message_id: str | None = None


@dataclass(frozen=True)
class RuntimeContext:
    """Runtime identity and filesystem context already resolved by the caller.

    ``default_working_dir`` is an optional, caller-supplied fallback for
    trusted embeddings and tests.  The engine never derives a workspace from
    its own process current directory.
    """

    agent_id: str
    agent_name: str
    profile_dir: Path
    agents_dir: Path
    session_id: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)
    identity_catalog: IdentityCatalog | None = None
    default_working_dir: Path | None = None


@dataclass
class RuntimeServices:
    """Per-request services owned by the caller and consumed by the engine."""

    llm: LLMPort
    tool_registry: ToolRegistry
    skill_registry: SkillRegistry
    gate_llm: LLMPort | None = None
    background_llm: LLMPort | None = None
    tool_guard: ToolGuard | None = None
    mcp_clients: list[Any] = field(default_factory=list)
    mcp_session_pool: Any | None = None
    owns_mcp_clients: bool = True
    hooks: HookManager | None = None
    observation_factory: RunObservationFactory | None = None
    owns_llm_clients: bool = True
    _memory_lifecycle_hook: Any | None = field(default=None, init=False, repr=False)
    _memory_lifecycle_hook_key: tuple[int, int, bool, int] | None = field(
        default=None, init=False, repr=False,
    )

    async def close(self) -> None:
        # 逐资源隔离：第一个 close 抛异常不许掐断其余资源的清理，
        # 否则 MCP 子进程/LLM 连接会在长期运行的进程里泄漏。
        cancellation: asyncio.CancelledError | None = None

        async def close_resource(resource: object, resource_type: str) -> None:
            nonlocal cancellation
            try:
                close = getattr(resource, "close", None)
                if close is None:
                    return
                result = close()
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
                logger.warning(
                    "cleanup of %s %s was cancelled; continuing remaining cleanup",
                    resource_type,
                    type(resource).__name__,
                )
            except Exception:
                logger.warning(
                    "failed to close %s %s",
                    resource_type,
                    type(resource).__name__,
                    exc_info=True,
                )

        if self.owns_mcp_clients:
            for client in reversed(self.mcp_clients):
                await close_resource(client, "MCP client")

        if self.owns_llm_clients:
            closed_llms: set[int] = set()
            for llm in (self.background_llm, self.gate_llm, self.llm):
                if llm is None or id(llm) in closed_llms:
                    continue
                closed_llms.add(id(llm))
                await close_resource(llm, "LLM client")

        if cancellation is not None:
            raise cancellation


@dataclass(frozen=True)
class EngineResult:
    text: str
    had_tools: bool = False
