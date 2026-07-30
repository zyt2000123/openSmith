from pathlib import Path

import pytest

from engine.execution.pipeline.skill_chain import (
    GateContentError,
    SkillChain,
    load_gate_content,
)
from engine.identity import IdentityCatalog

ROOT = Path(__file__).resolve().parents[3]
load_gate_content(ROOT / "agents")


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "pipeline.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_unknown_gate_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, "steps:\n  - skill: understand\n    gate: reviewww\n")
    with pytest.raises(ValueError, match="unknown gate"):
        SkillChain.from_yaml(path)


def test_unknown_condition_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "steps:\n  - skill: architecture\n    gate: design\n    condition: nope\n",
    )
    with pytest.raises(ValueError, match="unknown condition"):
        SkillChain.from_yaml(path)


def test_valid_pipeline_still_loads(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "steps:\n"
        "  - skill: understand\n"
        "    gate: understanding\n"
        "  - skill: architecture\n"
        "    gate: design\n"
        "    condition: coding_bugfix_needs_diagnosis\n",
    )
    chain = SkillChain.from_yaml(path)
    assert chain is not None
    assert [n.skill_name for n in chain.nodes] == ["understand", "architecture"]
    assert chain.nodes[1].condition is not None


def test_shipped_coding_skillchains_load_gates_conditions_and_pause_contracts() -> None:
    content = load_gate_content(ROOT / "agents")

    pipelines = SkillChain.load_pipelines(
        ROOT / "agents" / "pipelines",
        gate_registry=content.gates,
        condition_registry=content.conditions,
    )

    assert set(pipelines) == {
        "requirements-research",
        "tdd-development",
        "code-review",
    }
    requirements = pipelines["requirements-research"]
    assert [node.skill_name for node in requirements.nodes] == [
        "grilling",
        "research",
        "ecc-plan",
    ]
    assert requirements.nodes[0].await_user_input_marker
    assert requirements.nodes[0].instructions
    assert requirements.nodes[2].await_user_input_marker
    assert requirements.nodes[0].allowed_tools == (
        "read_file", "read_pdf", "render_pdf_page", "list_dir", "glob_files", "grep",
    )
    assert set(requirements.nodes[1].allowed_tools or ()) == {
        "read_file", "read_pdf", "render_pdf_page", "write_file", "list_dir",
        "glob_files", "grep", "web_search", "web_fetch",
    }
    assert "web_crawl" not in (requirements.nodes[1].allowed_tools or ())

    tdd = pipelines["tdd-development"]
    assert [node.skill_name for node in tdd.nodes] == [
        "diagnosing-bugs",
        "tdd-workflow",
        "verification-loop",
    ]
    assert tdd.nodes[0].condition is not None
    assert set(tdd.nodes[1].allowed_tools or ()) == {
        "read_file", "write_file", "edit_file", "list_dir", "glob_files", "grep", "shell",
    }
    assert "scripts/setup-package-manager.js" in tdd.nodes[1].instructions
    assert "bare `npx`" in tdd.nodes[1].instructions

    review = pipelines["code-review"]
    assert [node.skill_name for node in review.nodes] == [
        "code-review",
        "verification-loop",
    ]
    assert review.nodes[0].await_user_input_marker
    assert set(review.nodes[0].allowed_tools or ()) == {
        "read_file", "read_pdf", "render_pdf_page", "list_dir", "glob_files", "grep", "shell",
    }
    assert "write_file" not in (review.nodes[0].allowed_tools or ())
    assert "web_search" not in (review.nodes[0].allowed_tools or ())

    coding = next(
        identity
        for identity in IdentityCatalog.load(ROOT / "agents" / "identities").identities
        if identity.id == "coding"
    )
    node_tools = {
        tool_name
        for chain in pipelines.values()
        for node in chain.nodes
        for tool_name in (node.allowed_tools or ())
    }
    assert set(coding.enabled_tools or ()) == node_tools


def test_shipped_gate_and_condition_content_do_not_import_engine_or_common() -> None:
    content_files = [
        *(ROOT / "agents" / "gates").rglob("*.py"),
        *(ROOT / "agents" / "conditions").rglob("*.py"),
        *(ROOT / "agents" / "tools").glob("*.py"),
    ]

    for path in content_files:
        source = path.read_text(encoding="utf-8")
        assert "from engine" not in source
        assert "import engine" not in source
        assert "from common" not in source
        assert "import common" not in source


def test_malformed_pipeline_step_fails_loudly(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "steps:\n"
        "  - skill: understand\n"
        "  - skill: 42\n",
    )

    with pytest.raises(ValueError, match=r"steps\[1\].*skill"):
        SkillChain.from_yaml(path)


def test_node_pause_marker_and_instructions_are_parsed(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "steps:\n"
        "  - skill: interview\n"
        "    gate: rubric\n"
        "    await_user_input_marker: '<!-- wait -->'\n"
        "    instructions: |\n"
        "      Ask one question.\n",
    )
    chain = SkillChain.from_yaml(path)
    assert chain is not None
    assert chain.nodes[0].await_user_input_marker == "<!-- wait -->"
    assert chain.nodes[0].instructions == "Ask one question."


def test_node_tool_scope_is_parsed_and_rejects_invalid_values(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "steps:\n"
        "  - skill: interview\n"
        "    gate: rubric\n"
        "    allowed_tools: [read_file, shell]\n",
    )
    chain = SkillChain.from_yaml(path)
    assert chain is not None
    assert chain.nodes[0].allowed_tools == ("read_file", "shell")

    malformed = _write(
        tmp_path,
        "steps:\n"
        "  - skill: interview\n"
        "    gate: rubric\n"
        "    allowed_tools: [read_file, 42]\n",
    )
    with pytest.raises(ValueError, match=r"allowed_tools"):
        SkillChain.from_yaml(malformed)


def test_gate_defaults_to_rubric_when_omitted(tmp_path: Path) -> None:
    path = _write(tmp_path, "steps:\n  - skill: understand\n")
    chain = SkillChain.from_yaml(path)
    assert chain is not None
    assert chain.nodes[0].gate is not None


def test_base_gate_parsed_from_yaml(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "base_gate: rubric\n"
        "steps:\n  - skill: understand\n    gate: understanding\n",
    )
    chain = SkillChain.from_yaml(path)
    assert chain is not None
    assert len(chain.base_gates) == 1


def test_base_gates_list_parsed_from_yaml(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "base_gates: [rubric, understanding]\n"
        "steps:\n  - skill: understand\n    gate: understanding\n",
    )
    chain = SkillChain.from_yaml(path)
    assert chain is not None
    assert len(chain.base_gates) == 2


def test_base_gates_default_to_empty(tmp_path: Path) -> None:
    path = _write(tmp_path, "steps:\n  - skill: understand\n    gate: understanding\n")
    chain = SkillChain.from_yaml(path)
    assert chain is not None
    assert chain.base_gates == []


def test_unknown_base_gate_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "base_gate: nope\nsteps:\n  - skill: understand\n    gate: understanding\n",
    )
    with pytest.raises(ValueError, match="unknown gate"):
        SkillChain.from_yaml(path)


def test_custom_domain_gate_extends_registry_without_engine_change(tmp_path: Path) -> None:
    """新领域门禁 = 往 agents/gates/<domain>/ 放文件,零引擎改动。"""
    gates_dir = tmp_path / "gates" / "legal"
    gates_dir.mkdir(parents=True)
    (gates_dir / "gates.py").write_text(
        "class GateResult:\n"
        "    def __init__(self, verdict, reason, retry_hint=None):\n"
        "        self.verdict = verdict\n"
        "        self.reason = reason\n"
        "        self.retry_hint = retry_hint\n"
        "class ComplianceGate:\n"
        "    async def check(self, output, context):\n"
        "        return GateResult('pass', 'ok')\n"
        "GATES = {'compliance': ComplianceGate}\n",
        encoding="utf-8",
    )
    load_gate_content(tmp_path)
    path = _write(tmp_path, "steps:\n  - skill: contract-review\n    gate: compliance\n")
    chain = SkillChain.from_yaml(path)
    assert chain is not None
    assert chain.nodes[0].skill_name == "contract-review"


def test_content_conditions_receive_the_output_key_helper_without_importing_engine(tmp_path: Path) -> None:
    conditions_dir = tmp_path / "conditions"
    conditions_dir.mkdir()
    (conditions_dir / "conditions.py").write_text(
        "def has_plan(context):\n"
        "    return bool(context.get(output_key('planning')))\n"
        "CONDITIONS = {'has_plan': has_plan}\n",
        encoding="utf-8",
    )

    content = load_gate_content(tmp_path)
    assert content.conditions["has_plan"]({"planning_output": "a concrete plan"}) is True


def test_gate_content_registries_are_scoped_per_agents_directory(tmp_path: Path) -> None:
    def write_gate(root: Path, class_name: str) -> None:
        gates_dir = root / "gates"
        gates_dir.mkdir(parents=True)
        (gates_dir / "gates.py").write_text(
            "from engine.execution.pipeline.gate import GateResult\n"
            f"class {class_name}:\n"
            "    async def check(self, output, context):\n"
            "        return GateResult('pass', 'ok')\n"
            f"GATES = {{'shared': {class_name}}}\n",
            encoding="utf-8",
        )

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    write_gate(first_root, "FirstGate")
    write_gate(second_root, "SecondGate")

    first = load_gate_content(first_root)
    second = load_gate_content(second_root)

    first_path = _write(
        first_root,
        "base_gate: shared\nsteps:\n  - skill: one\n    gate: shared\n",
    )
    second_path = _write(
        second_root,
        "base_gate: shared\nsteps:\n  - skill: two\n    gate: shared\n",
    )
    first_chain = SkillChain.from_yaml(
        first_path,
        gate_registry=first.gates,
        condition_registry=first.conditions,
    )
    second_chain = SkillChain.from_yaml(
        second_path,
        gate_registry=second.gates,
        condition_registry=second.conditions,
    )

    assert type(first_chain.nodes[0].gate).__name__ == "FirstGate"
    assert type(second_chain.nodes[0].gate).__name__ == "SecondGate"
    assert type(first_chain.base_gates[0]).__name__ == "FirstGate"
    assert type(second_chain.base_gates[0]).__name__ == "SecondGate"


def test_duplicate_gate_key_across_files_raises(tmp_path: Path) -> None:
    gates_dir = tmp_path / "gates"
    gates_dir.mkdir()
    body = (
        "from engine.execution.pipeline.gate import GateResult\n"
        "class G:\n"
        "    async def check(self, output, context):\n"
        "        return GateResult('pass', 'ok')\n"
        "GATES = {'dup_gate_key': G}\n"
    )
    (gates_dir / "a.py").write_text(body, encoding="utf-8")
    (gates_dir / "b.py").write_text(body.replace("class G", "class H").replace("': G", "': H"), encoding="utf-8")
    with pytest.raises(GateContentError, match="duplicate"):
        load_gate_content(tmp_path)


def test_broken_gate_content_fails_loudly(tmp_path: Path) -> None:
    gates_dir = tmp_path / "gates"
    gates_dir.mkdir()
    (gates_dir / "broken.py").write_text("this is not python ((", encoding="utf-8")
    with pytest.raises(GateContentError, match="failed to load"):
        load_gate_content(tmp_path)
