"""Validation of assets referenced by the execution domain."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from engine.identity import IdentityCatalog

from .pipeline.skill_chain import SkillChain, load_gate_content


def validate_execution_assets(
    catalog: IdentityCatalog,
    *,
    agents_dir: Path,
    skill_names: Iterable[str],
) -> None:
    """Validate identity routes without exposing pipeline implementation types."""
    gate_content = load_gate_content(agents_dir)
    pipelines = SkillChain.load_pipelines(
        agents_dir / "pipelines",
        gate_registry=gate_content.gates,
        condition_registry=gate_content.conditions,
    )
    catalog.validate_assets(pipelines.keys(), skill_names)


__all__ = ("validate_execution_assets",)
