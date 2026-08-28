"""Declarative sub-agent types.

A sub-agent type is a named *capability envelope*: a system prompt, a tool
allowlist, and an iteration ceiling.  It is not a separate running agent and
owns no profile record — the engine spawns one isolated ReAct conversation per
task and keeps only the returned summary.

Content authors add types as YAML files under ``agents/subagents``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from common.yaml_utils import YamlConfigError, load_yaml

SUBAGENT_SCHEMA = "agentsmith.subagent/v1"

# A sub-agent must never spawn sub-agents: unbounded recursion would multiply
# provider spend with no ceiling the parent can see.  Enforced here (stripped
# from every declared tool list) *and* again in the runner, because a catalog
# can be constructed directly in tests and embeddings.
SUB_AGENT_TOOL_NAME = "sub_agent"

# Ceilings the content layer cannot raise.  A YAML author who asks for 500
# iterations is asking for a runaway bill, not a capability.
MAX_DECLARED_ITERS = 40
DEFAULT_DECLARED_ITERS = 15
# A hard ceiling on one sub-agent's provider spend.  The iteration cap
# bounds *turns*; this bounds tokens, which is what a runaway actually
# burns when each turn carries a large tool result.
MAX_DECLARED_TOKEN_BUDGET = 400_000
DEFAULT_TOKEN_BUDGET = 120_000

# Which of the engine's already-constructed ports a type runs on.  Content
# names a *role*, never a provider or a model string: credentials and model
# selection stay in the operator's config, out of shipped YAML.
SUBAGENT_MODEL_ROLES = frozenset({"interactive", "background"})
DEFAULT_MODEL_ROLE = "interactive"


class SubAgentCatalogError(ValueError):
    """Raised when declarative sub-agent content is invalid."""


@dataclass(frozen=True)
class SubAgentSpec:
    """One validated sub-agent type."""

    id: str
    name: str
    description: str
    prompt: str
    tools: tuple[str, ...]
    max_iters: int = DEFAULT_DECLARED_ITERS
    model: str = DEFAULT_MODEL_ROLE
    token_budget: int = DEFAULT_TOKEN_BUDGET


def _non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SubAgentCatalogError(f"{label} must be a non-empty string")
    return value.strip()


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise SubAgentCatalogError(f"{label} must be a list of strings")
    return tuple(
        _non_empty_string(item, f"{label}[{index}]")
        for index, item in enumerate(value)
    )


def _parse_spec(path: Path) -> SubAgentSpec:
    try:
        raw = load_yaml(path)
    except YamlConfigError as exc:
        raise SubAgentCatalogError(str(exc)) from exc
    if not raw:
        raise SubAgentCatalogError(f"Sub-agent document {path} is empty")
    if not isinstance(raw, Mapping):
        raise SubAgentCatalogError(f"Sub-agent document {path} must be a mapping")
    allowed = {
        "schema",
        "id",
        "name",
        "description",
        "prompt",
        "tools",
        "max_iters",
        "model",
        "token_budget",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise SubAgentCatalogError(
            f"Sub-agent document {path} has unknown fields: {', '.join(sorted(unknown))}"
        )
    schema = _non_empty_string(raw.get("schema"), f"Sub-agent document {path}.schema")
    if schema != SUBAGENT_SCHEMA:
        raise SubAgentCatalogError(
            f"Sub-agent document {path} must use schema {SUBAGENT_SCHEMA!r}"
        )
    spec_id = _non_empty_string(raw.get("id"), f"Sub-agent document {path}.id")
    declared = _string_list(raw.get("tools"), f"Sub-agent document {path}.tools")
    # Recursion is stripped rather than rejected: a content author copying a
    # broad tool list should get a working type, not a load failure that takes
    # the whole catalog down with it.  Validated *after* the strip — checking
    # the declared list would let ``tools: [sub_agent]`` pass as "at least one
    # tool" and then produce a type with none.
    tools = tuple(name for name in declared if name != SUB_AGENT_TOOL_NAME)
    if not tools:
        raise SubAgentCatalogError(
            f"Sub-agent document {path}.tools must list at least one tool "
            f"other than {SUB_AGENT_TOOL_NAME!r}"
        )
    max_iters = raw.get("max_iters", DEFAULT_DECLARED_ITERS)
    if isinstance(max_iters, bool) or not isinstance(max_iters, int) or max_iters < 1:
        raise SubAgentCatalogError(
            f"Sub-agent document {path}.max_iters must be a positive integer"
        )
    model = raw.get("model", DEFAULT_MODEL_ROLE)
    if model not in SUBAGENT_MODEL_ROLES:
        raise SubAgentCatalogError(
            f"Sub-agent document {path}.model must be one of "
            f"{', '.join(sorted(SUBAGENT_MODEL_ROLES))}"
        )
    token_budget = raw.get("token_budget", DEFAULT_TOKEN_BUDGET)
    if (
        isinstance(token_budget, bool)
        or not isinstance(token_budget, int)
        or token_budget < 1
    ):
        raise SubAgentCatalogError(
            f"Sub-agent document {path}.token_budget must be a positive integer"
        )
    return SubAgentSpec(
        id=spec_id,
        name=_non_empty_string(raw.get("name"), f"Sub-agent document {path}.name"),
        description=_non_empty_string(
            raw.get("description"), f"Sub-agent document {path}.description"
        ),
        prompt=_non_empty_string(raw.get("prompt"), f"Sub-agent document {path}.prompt"),
        tools=tools,
        max_iters=min(max_iters, MAX_DECLARED_ITERS),
        model=model,
        token_budget=min(token_budget, MAX_DECLARED_TOKEN_BUDGET),
    )


class SubAgentCatalog:
    """One validated catalog of sub-agent types."""

    def __init__(self, specs: Iterable[SubAgentSpec]) -> None:
        self._specs = tuple(specs)
        self._by_id = {spec.id: spec for spec in self._specs}
        if len(self._by_id) != len(self._specs):
            raise SubAgentCatalogError("Sub-agent catalog has duplicate ids")

    @classmethod
    def load(cls, subagents_dir: Path) -> "SubAgentCatalog":
        """Load every YAML type in *subagents_dir*; a missing directory is empty.

        An absent directory disables the feature rather than failing the run:
        sub-agents are optional content, and an install that ships none must
        still start.
        """
        if not subagents_dir.is_dir():
            return cls(())
        paths = sorted({*subagents_dir.glob("*.yaml"), *subagents_dir.glob("*.yml")})
        return cls(_parse_spec(path) for path in paths)

    def __bool__(self) -> bool:
        return bool(self._specs)

    @property
    def specs(self) -> tuple[SubAgentSpec, ...]:
        return self._specs

    def ids(self) -> tuple[str, ...]:
        return tuple(spec.id for spec in self._specs)

    def get(self, spec_id: str) -> SubAgentSpec:
        try:
            return self._by_id[spec_id]
        except KeyError as exc:
            known = ", ".join(self.ids()) or "(none)"
            raise SubAgentCatalogError(
                f"Unknown sub-agent type {spec_id!r}; available: {known}"
            ) from exc

    def describe(self) -> str:
        """One line per type, for the parent model's tool description."""
        return "\n".join(
            f"- {spec.id}: {spec.description} (tools: {', '.join(spec.tools)})"
            for spec in self._specs
        )


__all__ = (
    "DEFAULT_DECLARED_ITERS",
    "DEFAULT_MODEL_ROLE",
    "DEFAULT_TOKEN_BUDGET",
    "MAX_DECLARED_ITERS",
    "MAX_DECLARED_TOKEN_BUDGET",
    "SUBAGENT_MODEL_ROLES",
    "SUBAGENT_SCHEMA",
    "SUB_AGENT_TOOL_NAME",
    "SubAgentCatalog",
    "SubAgentCatalogError",
    "SubAgentSpec",
)
