"""Declarative domain identities and their intent-to-pipeline routes.

An identity is a capability profile of the one resident Smith agent. It is
not a separately running agent and never owns a separate server-side profile
record. Content authors extend Smith with YAML files under ``agents/identities``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from common.yaml_utils import YamlConfigError, load_yaml


IDENTITY_SCHEMA = "agentsmith.identity/v1"


class IdentityCatalogError(ValueError):
    """Raised when declarative identity content is invalid or inconsistent."""


@dataclass(frozen=True)
class RouteSpec:
    """One intent route declared by an identity."""

    id: str
    pipeline: str | None
    keywords: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    priority: int = 0


@dataclass(frozen=True)
class IdentitySpec:
    """A domain profile that can be selected for one Smith run."""

    id: str
    name: str
    description: str
    prompt: str
    enabled_tools: tuple[str, ...] | None
    enabled_skills: tuple[str, ...] | None
    routes: tuple[RouteSpec, ...]
    is_default: bool = False


@dataclass(frozen=True)
class RouteDecision:
    """The complete, validated result passed from routing to execution."""

    identity: IdentitySpec
    route_id: str
    pipeline_id: str | None
    score: int = 0

    @property
    def identity_id(self) -> str:
        return self.identity.id

    def to_event_data(self) -> dict[str, object]:
        return {
            "identity_id": self.identity.id,
            "identity_name": self.identity.name,
            "route_id": self.route_id,
            "pipeline_id": self.pipeline_id,
            "score": self.score,
        }


def _non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IdentityCatalogError(f"{label} must be a non-empty string")
    return value.strip()


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise IdentityCatalogError(f"{label} must be a list of strings")
    return tuple(_non_empty_string(item, f"{label}[{index}]") for index, item in enumerate(value))


def _optional_enabled_list(value: object, label: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise IdentityCatalogError(f"{label} must be a mapping")
    unknown = set(value) - {"enabled"}
    if unknown:
        raise IdentityCatalogError(f"{label} has unknown fields: {', '.join(sorted(unknown))}")
    enabled = value.get("enabled")
    return None if enabled is None else _string_list(enabled, f"{label}.enabled")


def _prompt_text(value: object, label: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, Mapping):
        raise IdentityCatalogError(f"{label} must be a string or a mapping")
    unknown = set(value) - {"role", "style", "instructions"}
    if unknown:
        raise IdentityCatalogError(f"{label} has unknown fields: {', '.join(sorted(unknown))}")
    sections: list[str] = []
    for key, heading in (("role", "Role"), ("style", "Style"), ("instructions", "Instructions")):
        item = value.get(key)
        if item is not None:
            sections.append(f"## Active Identity {heading}\n{_non_empty_string(item, f'{label}.{key}')}")
    return "\n\n".join(sections)


def _parse_route(raw: object, identity_id: str, index: int) -> RouteSpec:
    label = f"identity {identity_id!r}.routes[{index}]"
    if not isinstance(raw, Mapping):
        raise IdentityCatalogError(f"{label} must be a mapping")
    unknown = set(raw) - {"id", "pipeline", "keywords", "examples", "priority"}
    if unknown:
        raise IdentityCatalogError(f"{label} has unknown fields: {', '.join(sorted(unknown))}")
    route_id = _non_empty_string(raw.get("id"), f"{label}.id")
    pipeline_value = raw.get("pipeline")
    pipeline = None if pipeline_value is None else _non_empty_string(pipeline_value, f"{label}.pipeline")
    keywords = _string_list(raw.get("keywords"), f"{label}.keywords")
    examples = _string_list(raw.get("examples"), f"{label}.examples")
    priority = raw.get("priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise IdentityCatalogError(f"{label}.priority must be an integer")
    # A declared route may intentionally remain direct.  This lets narrow,
    # operational intents (for example Git commands) avoid an unrelated
    # multi-skill implementation pipeline while still selecting the identity's
    # normal tools and safety checks.
    return RouteSpec(route_id, pipeline, keywords, examples, priority)


def _parse_identity(path: Path) -> IdentitySpec:
    try:
        raw = load_yaml(path)
    except YamlConfigError as exc:
        raise IdentityCatalogError(str(exc)) from exc
    if not raw:
        raise IdentityCatalogError(f"Identity document {path} is empty")
    allowed = {"schema", "id", "name", "description", "default", "prompt", "tools", "skills", "routes"}
    unknown = set(raw) - allowed
    if unknown:
        raise IdentityCatalogError(f"Identity document {path} has unknown fields: {', '.join(sorted(unknown))}")
    schema = _non_empty_string(raw.get("schema"), f"Identity document {path}.schema")
    if schema != IDENTITY_SCHEMA:
        raise IdentityCatalogError(f"Identity document {path} must use schema {IDENTITY_SCHEMA!r}")
    identity_id = _non_empty_string(raw.get("id"), f"Identity document {path}.id")
    name = _non_empty_string(raw.get("name"), f"Identity document {path}.name")
    description = str(raw.get("description") or "").strip()
    default = raw.get("default", False)
    if not isinstance(default, bool):
        raise IdentityCatalogError(f"Identity document {path}.default must be a boolean")
    routes_value = raw.get("routes", [])
    if not isinstance(routes_value, list):
        raise IdentityCatalogError(f"Identity document {path}.routes must be a list")
    routes = tuple(_parse_route(route, identity_id, index) for index, route in enumerate(routes_value))
    route_ids = [route.id for route in routes]
    if len(route_ids) != len(set(route_ids)):
        raise IdentityCatalogError(f"Identity document {path} has duplicate route ids")
    return IdentitySpec(
        id=identity_id,
        name=name,
        description=description,
        prompt=_prompt_text(raw.get("prompt"), f"Identity document {path}.prompt"),
        enabled_tools=_optional_enabled_list(raw.get("tools"), f"Identity document {path}.tools"),
        enabled_skills=_optional_enabled_list(raw.get("skills"), f"Identity document {path}.skills"),
        routes=routes,
        is_default=default,
    )


class IdentityCatalog:
    """One validated catalog of Smith's declarative domain profiles."""

    def __init__(self, identities: Iterable[IdentitySpec]) -> None:
        self._identities = tuple(identities)
        if not self._identities:
            raise IdentityCatalogError("Identity catalog must contain at least one identity")
        self._by_id = {identity.id: identity for identity in self._identities}
        if len(self._by_id) != len(self._identities):
            raise IdentityCatalogError("Identity catalog has duplicate identity ids")
        defaults = [identity for identity in self._identities if identity.is_default]
        if len(defaults) != 1:
            raise IdentityCatalogError("Identity catalog must declare exactly one default identity")
        self.default = defaults[0]

    @classmethod
    def load(cls, identities_dir: Path) -> "IdentityCatalog":
        if not identities_dir.is_dir():
            raise IdentityCatalogError(f"Identity directory does not exist: {identities_dir}")
        paths = sorted({*identities_dir.glob("*.yaml"), *identities_dir.glob("*.yml")})
        if not paths:
            raise IdentityCatalogError(f"Identity directory contains no YAML documents: {identities_dir}")
        return cls(_parse_identity(path) for path in paths)

    @property
    def identities(self) -> tuple[IdentitySpec, ...]:
        return self._identities

    def get(self, identity_id: str) -> IdentitySpec:
        try:
            return self._by_id[identity_id]
        except KeyError as exc:
            raise IdentityCatalogError(f"Unknown identity {identity_id!r}") from exc

    def resolve(self, message: str, identity_id: str | None = None) -> RouteDecision:
        """Return the highest-confidence declared route, or the default direct mode."""
        if identity_id:
            return self._resolve_identity(self.get(identity_id), message)
        candidates: list[tuple[int, int, int, IdentitySpec, RouteSpec]] = []
        for identity_index, identity in enumerate(self._identities):
            for route_index, route in enumerate(identity.routes):
                score = self._score(route, message)
                if score > 0:
                    candidates.append((score, route.priority, -identity_index * 1000 - route_index, identity, route))
        if not candidates:
            return RouteDecision(self.default, "direct", None)
        score, _priority, _order, identity, route = max(candidates, key=lambda candidate: candidate[:3])
        return RouteDecision(identity, route.id, route.pipeline, score)

    def validate_assets(
        self,
        pipeline_ids: Iterable[str],
        skill_names: Iterable[str],
    ) -> None:
        """Fail startup if a declared identity references unavailable assets.

        Pipeline nodes may intentionally fall back to generic ReAct execution
        when a specialized SKILL.md is not installed, so pipeline-internal skill
        names are not rejected here. An identity's explicit skill allowlist is
        different: it is a hard security/capability declaration and must exist.
        """
        pipelines = set(pipeline_ids)
        skills = set(skill_names)
        for identity in self._identities:
            if identity.enabled_skills is not None:
                missing_allowed = set(identity.enabled_skills) - skills
                if missing_allowed:
                    names = ", ".join(sorted(missing_allowed))
                    raise IdentityCatalogError(f"Identity {identity.id!r} enables unknown skills: {names}")
            for route in identity.routes:
                if route.pipeline is None:
                    continue
                if route.pipeline not in pipelines:
                    raise IdentityCatalogError(
                        f"Identity {identity.id!r} route {route.id!r} references unknown pipeline {route.pipeline!r}"
                    )

    @staticmethod
    def _score(route: RouteSpec, message: str) -> int:
        normalized = message.casefold()
        score = 0
        for example in route.examples:
            if example.casefold() in normalized:
                score += 10
        for keyword in route.keywords:
            if keyword.casefold() in normalized:
                score += 3
        return score

    @staticmethod
    def _resolve_identity(identity: IdentitySpec, message: str) -> RouteDecision:
        candidates: list[tuple[int, int, int, RouteSpec]] = []
        for index, route in enumerate(identity.routes):
            score = IdentityCatalog._score(route, message)
            if score > 0:
                candidates.append((score, route.priority, -index, route))
        if not candidates:
            return RouteDecision(identity, "direct", None)
        score, _priority, _index, route = max(candidates, key=lambda candidate: candidate[:3])
        return RouteDecision(identity, route.id, route.pipeline, score)


_cached_catalog: IdentityCatalog | None = None
_cached_root: Path | None = None


def load_identity_catalog(identities_dir: Path, *, force: bool = False) -> IdentityCatalog:
    """Load once per process; FastAPI calls this during startup to fail early."""
    global _cached_catalog, _cached_root
    root = identities_dir.resolve()
    if force or _cached_catalog is None or _cached_root != root:
        _cached_catalog = IdentityCatalog.load(root)
        _cached_root = root
    return _cached_catalog
