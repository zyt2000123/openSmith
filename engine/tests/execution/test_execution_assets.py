from __future__ import annotations

from pathlib import Path

import pytest

from engine.execution import validate_execution_assets
from engine.identity import IdentityCatalog, IdentityCatalogError
from engine.tool.registry import ToolRegistry


def test_shipped_coding_identity_assets_form_a_closed_startup_bundle() -> None:
    """The published coding identity is valid before a server accepts traffic."""
    agents_dir = Path(__file__).resolve().parents[3] / "agents"
    tool_registry = ToolRegistry()
    tool_registry.load_builtin_providers(agents_dir / "tools")
    skill_names = {
        skill_file.parent.name
        for skill_file in (agents_dir / "skills").glob("*/SKILL.md")
    }

    validate_execution_assets(
        IdentityCatalog.load(agents_dir / "identities"),
        agents_dir=agents_dir,
        skill_names=skill_names,
        tool_names=tool_registry.list_tool_names(),
    )


def test_shipped_coding_identity_rejects_a_missing_declared_chain_skill() -> None:
    """A broken chain dependency must fail validation instead of degrading to ReAct."""
    agents_dir = Path(__file__).resolve().parents[3] / "agents"
    catalog = IdentityCatalog.load(agents_dir / "identities")
    skill_names = {
        skill_file.parent.name
        for skill_file in (agents_dir / "skills").glob("*/SKILL.md")
    }

    with pytest.raises(
        IdentityCatalogError,
        match="grilling",
    ):
        validate_execution_assets(
            catalog,
            agents_dir=agents_dir,
            skill_names=skill_names - {"grilling"},
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


def test_pipeline_route_rejects_a_tool_outside_the_identity_allowlist(
    tmp_path: Path,
) -> None:
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
tools:
  enabled: [read_file]
skills:
  enabled: [required-stage]
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
    allowed_tools: [shell]
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

    with pytest.raises(IdentityCatalogError, match="tools outside its allowlist: shell"):
        validate_execution_assets(
            IdentityCatalog.load(identities_dir),
            agents_dir=tmp_path,
            skill_names={"required-stage"},
        )


def test_pipeline_route_rejects_a_declared_tool_missing_from_runtime(
    tmp_path: Path,
) -> None:
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
tools:
  enabled: [shell]
skills:
  enabled: [required-stage]
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
    allowed_tools: [shell]
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

    with pytest.raises(IdentityCatalogError, match="requires unavailable tools: shell"):
        validate_execution_assets(
            IdentityCatalog.load(identities_dir),
            agents_dir=tmp_path,
            skill_names={"required-stage"},
            tool_names={"read_file"},
        )
