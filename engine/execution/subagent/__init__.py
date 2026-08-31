"""Sub-agent orchestration: isolated delegated runs that return a summary."""

from .catalog import (
    SUB_AGENT_TOOL_NAME,
    SUBAGENT_SCHEMA,
    SubAgentCatalog,
    SubAgentCatalogError,
    SubAgentSpec,
)
from .runner import (
    DEFAULT_BATCH_TOKEN_BUDGET,
    DEFAULT_MAX_PARALLEL,
    MAX_TASKS_PER_CALL,
    SubAgentOutcome,
    SubAgentTask,
    run_sub_agents,
)

__all__ = (
    "DEFAULT_BATCH_TOKEN_BUDGET",
    "DEFAULT_MAX_PARALLEL",
    "MAX_TASKS_PER_CALL",
    "SUBAGENT_SCHEMA",
    "SUB_AGENT_TOOL_NAME",
    "SubAgentCatalog",
    "SubAgentCatalogError",
    "SubAgentOutcome",
    "SubAgentSpec",
    "SubAgentTask",
    "run_sub_agents",
)
