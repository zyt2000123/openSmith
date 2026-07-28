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
) -> None:
    """Validate every shipped identity's executable asset closure.

    A route that names a pipeline is a promise that the pipeline can actually
    run.  Checking only the YAML filename leaves a deployment with a missing
    stage to silently fall back to generic ReAct at request time.  Keep this
    check at startup, where the content catalog, parsed chain, and registered
    skill names are all available together.
    """
    available_skills = set(skill_names)
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


__all__ = ("validate_execution_assets",)
