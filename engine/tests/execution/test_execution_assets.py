from __future__ import annotations

from pathlib import Path

import pytest

from engine.execution import validate_execution_assets
from engine.identity import IdentityCatalog, IdentityCatalogError


def test_shipped_coding_identity_assets_form_a_closed_startup_bundle() -> None:
    """The published coding identity is valid before a server accepts traffic."""
    agents_dir = Path(__file__).resolve().parents[3] / "agents"
    skill_names = {
        skill_file.parent.name
        for skill_file in (agents_dir / "skills").glob("*/SKILL.md")
    }

    validate_execution_assets(
        IdentityCatalog.load(agents_dir / "identities"),
        agents_dir=agents_dir,
        skill_names=skill_names,
    )


def test_shipped_coding_identity_rejects_a_missing_declared_stage_skill() -> None:
    """A broken coding plugin must fail validation instead of silently degrading."""
    agents_dir = Path(__file__).resolve().parents[3] / "agents"
    catalog = IdentityCatalog.load(agents_dir / "identities")
    skill_names = {
        skill_file.parent.name
        for skill_file in (agents_dir / "skills").glob("*/SKILL.md")
    }

    with pytest.raises(
        IdentityCatalogError,
        match="coding-validation",
    ):
        validate_execution_assets(
            catalog,
            agents_dir=agents_dir,
            skill_names=skill_names - {"coding-validation"},
        )


def test_pipeline_route_rejects_a_stage_not_present_in_the_skill_registry(
    tmp_path: Path,
) -> None:
    """Pipeline nodes are execution requirements, even without an allowlist."""
    identities_dir = tmp_path / "identities"
    pipelines_dir = tmp_path / "pipelines"
    gates_dir = tmp_path / "gates"
    identities_dir.mkdir()
    pipelines_dir.mkdir()
    gates_dir.mkdir()
    (identities_dir / "smith.yaml").write_text(
        """
schema: agentsmith.identity/v1
id: smith
name: Smith
default: true
routes:
  - id: coding
    keywords: [implement]
    pipeline: coding
""".strip(),
        encoding="utf-8",
    )
    (pipelines_dir / "coding.yaml").write_text(
        """
route: coding
steps:
  - skill: required-stage
    gate: always_pass
""".strip(),
        encoding="utf-8",
    )
    (gates_dir / "always.py").write_text(
        """
class AlwaysPass:
    async def check(self, output, context):
        return "pass"

GATES = {"always_pass": AlwaysPass}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(IdentityCatalogError, match="required-stage"):
        validate_execution_assets(
            IdentityCatalog.load(identities_dir),
            agents_dir=tmp_path,
            skill_names=(),
        )
