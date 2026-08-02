"""Validation of assets referenced by the execution domain."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from engine.identity import IdentityCatalog, IdentityCatalogError

from .pipeline.skill_chain import SkillChain, load_gate_content


def validate_execution_assets(
    catalog: IdentityCatalog,
    *,
    agents_dir: Path,
    skill_names: Iterable[str],
    tool_names: Iterable[str] | None = None,
) -> None:
    """Validate every shipped identity's executable asset closure.

    A route that names a pipeline is a promise that the pipeline can actually
    run. Checking only the YAML filename would defer a missing stage until the
    request reaches its blocked node. Keep this check at startup, where the
    content catalog, parsed chain, and registered skill names are all available
    together.
    """
    available_skills = set(skill_names)
    available_tools = set(tool_names) if tool_names is not None else None
    gate_content = load_gate_content(agents_dir)
    pipelines = SkillChain.load_pipelines(
        agents_dir / "pipelines",
        gate_registry=gate_content.gates,
        condition_registry=gate_content.conditions,
    )
    catalog.validate_assets(pipelines.keys(), available_skills)

    for identity in catalog.identities:
        allowed_skills = (
            set(identity.enabled_skills)
            if identity.enabled_skills is not None
            else None
        )
        allowed_tools = (
            set(identity.enabled_tools)
            if identity.enabled_tools is not None
            else None
        )
        for route in identity.routes:
            if route.pipeline is None:
                continue
            chain = pipelines[route.pipeline]
            required_skills = {node.skill_name for node in chain.nodes}
            missing_skills = required_skills - available_skills
            if missing_skills:
                names = ", ".join(sorted(missing_skills))
                raise IdentityCatalogError(
                    f"Identity {identity.id!r} route {route.id!r} pipeline "
                    f"{route.pipeline!r} requires unavailable skills: {names}"
                )
            if allowed_skills is not None:
                undeclared_skills = required_skills - allowed_skills
                if undeclared_skills:
                    names = ", ".join(sorted(undeclared_skills))
                    raise IdentityCatalogError(
                        f"Identity {identity.id!r} route {route.id!r} pipeline "
                        f"{route.pipeline!r} uses skills outside its allowlist: {names}"
                    )
            required_tools = {
                tool_name
                for node in chain.nodes
                for tool_name in (node.allowed_tools or ())
            }
            if available_tools is not None:
                missing_tools = required_tools - available_tools
                if missing_tools:
                    names = ", ".join(sorted(missing_tools))
                    raise IdentityCatalogError(
                        f"Identity {identity.id!r} route {route.id!r} pipeline "
                        f"{route.pipeline!r} requires unavailable tools: {names}"
                    )
            if allowed_tools is not None:
                undeclared_tools = required_tools - allowed_tools
                if undeclared_tools:
                    names = ", ".join(sorted(undeclared_tools))
                    raise IdentityCatalogError(
                        f"Identity {identity.id!r} route {route.id!r} pipeline "
                        f"{route.pipeline!r} uses tools outside its allowlist: {names}"
                    )


__all__ = ("validate_execution_assets",)
