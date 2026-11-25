"""Executable architecture rules for the Engine package.

These tests protect dependency direction and public seams.  They intentionally
inspect imports and package layout rather than implementation details.
"""

from __future__ import annotations

import ast
import importlib
import tomllib
from pathlib import Path


ENGINE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = ENGINE_ROOT.parent


def _resolve_import_from(path: Path, node: ast.ImportFrom, *, repo_root: Path = REPO_ROOT) -> list[str]:
    """Resolve both absolute and relative ``from ... import ...`` statements (L8)."""
    if node.level == 0:
        return [node.module] if node.module else []
    try:
        rel_parts = list(path.relative_to(repo_root).with_suffix("").parts)
    except ValueError:
        return [node.module] if node.module else []
    if rel_parts and rel_parts[-1] == "__init__":
        package_parts = rel_parts[:-1]
    else:
        package_parts = rel_parts[:-1]
    ascend = node.level - 1
    if ascend > len(package_parts):
        base_parts: list[str] = []
    else:
        base_parts = package_parts[: len(package_parts) - ascend]
    if node.module:
        return [".".join([*base_parts, node.module])]
    resolved: list[str] = []
    for alias in node.names:
        resolved.append(".".join([*base_parts, alias.name]))
    return resolved


def _python_imports(root: Path, *, repo_root: Path = REPO_ROOT) -> list[tuple[Path, int, str]]:
    imports: list[tuple[Path, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in {".venv", "__pycache__"} for part in path.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for module in _resolve_import_from(path, node, repo_root=repo_root):
                    if module:
                        imports.append((path, node.lineno, module))
            elif isinstance(node, ast.Import):
                imports.extend((path, node.lineno, alias.name) for alias in node.names)
    return imports


def _relative_imports(
    imports: list[tuple[Path, int, str]],
    *,
    root: Path,
) -> list[str]:
    return [
        f"{path.relative_to(root)}:{line} imports {module}"
        for path, line, module in imports
    ]


def test_execution_package_exposes_the_external_run_interface() -> None:
    execution = importlib.import_module("engine.execution")

    expected = {
        "AgentRunStream",
        "EngineRequest",
        "EventType",
        "ExecutionEvent",
        "RunStateError",
        "RunStateStore",
        "RunStatus",
        "RuntimeContext",
        "RuntimeServices",
        "raw_text_delta",
        "reply_with_runtime",
        "resume_stream_with_runtime",
        "run_memory_idle_tick",
        "run_stream_with_runtime",
        "validate_execution_assets",
    }

    assert expected <= set(execution.__all__)
    assert all(hasattr(execution, name) for name in expected)
    assert not {
        "SkillChain",
        "load_gate_content",
        "smith_ui_fallback",
        "validate_smith_ui_call",
    } & set(execution.__all__)


def test_server_only_crosses_the_public_execution_seam() -> None:
    imports = [
        item
        for item in _python_imports(REPO_ROOT / "server")
        if item[2].startswith("engine.execution.")
    ]

    assert not imports, _relative_imports(imports, root=REPO_ROOT)


def test_leaf_packages_do_not_import_execution_implementation() -> None:
    imports: list[tuple[Path, int, str]] = []
    for package in ("mcp", "skill", "tool"):
        imports.extend(
            item
            for item in _python_imports(ENGINE_ROOT / package)
            if item[2].startswith("engine.execution")
        )

    assert not imports, _relative_imports(imports, root=REPO_ROOT)


def test_execution_owns_its_event_contract() -> None:
    assert (ENGINE_ROOT / "execution" / "events.py").is_file()
    assert not (ENGINE_ROOT / "observability" / "events.py").exists()

    observability = importlib.import_module("engine.observability")
    assert not hasattr(observability, "EventType")
    assert not hasattr(observability, "ExecutionEvent")
    assert not hasattr(observability, "RunObservationContext")
    assert not hasattr(observability, "raw_text_delta")


def test_execution_does_not_depend_on_observability_implementation() -> None:
    imports = [
        item
        for item in _python_imports(ENGINE_ROOT / "execution")
        if item[2].startswith("engine.observability")
    ]

    assert not imports, _relative_imports(imports, root=REPO_ROOT)


def test_execution_uses_runtime_inputs_instead_of_application_config() -> None:
    imports = [
        item
        for item in _python_imports(ENGINE_ROOT / "execution")
        if item[2] == "common.config"
    ]

    assert not imports, _relative_imports(imports, root=REPO_ROOT)


def test_llm_does_not_depend_on_execution_implementation() -> None:
    imports = [
        item
        for item in _python_imports(ENGINE_ROOT / "llm")
        if item[2].startswith("engine.execution")
    ]

    assert not imports, _relative_imports(imports, root=REPO_ROOT)


def test_runtime_helpers_live_with_their_owning_packages() -> None:
    moves = {
        ENGINE_ROOT / "identity_catalog.py": ENGINE_ROOT / "identity" / "catalog.py",
        ENGINE_ROOT / "hook.py": ENGINE_ROOT / "execution" / "hooks" / "__init__.py",
        ENGINE_ROOT / "replay.py": ENGINE_ROOT / "llm" / "replay.py",
        ENGINE_ROOT / "snapshot.py": ENGINE_ROOT / "tool" / "snapshot.py",
        (
            ENGINE_ROOT / "execution" / "memory" / "memory_maintenance.py"
        ): ENGINE_ROOT / "memory" / "maintenance.py",
        (
            ENGINE_ROOT / "execution" / "tool_execution" / "tool_ledger.py"
        ): ENGINE_ROOT / "tool" / "ledger.py",
    }

    for old_path, new_path in moves.items():
        assert not old_path.exists(), f"obsolete module remains: {old_path}"
        assert new_path.is_file(), f"owned module missing: {new_path}"


def test_lifecycle_delegates_runtime_preparation() -> None:
    agent_loop = importlib.import_module("engine.execution.orchestration.agent_loop")
    lifecycle = importlib.import_module("engine.execution.orchestration.lifecycle")
    preparation = importlib.import_module("engine.execution.orchestration.preparation")

    assert lifecycle.prepare_runtime is preparation.prepare_runtime
    assert not hasattr(agent_loop, "prepare_runtime")


def test_setuptools_uses_package_discovery() -> None:
    config = tomllib.loads((ENGINE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools = config["tool"]["setuptools"]
    packages = setuptools["packages"]

    assert isinstance(packages, dict)
    assert packages["find"]["where"] == [".."]
    assert packages["find"]["include"] == ["engine*"]
    assert packages["find"]["exclude"] == ["engine.tests*"]
    assert packages["find"]["namespaces"] is False
    assert setuptools["package-dir"] == {"": ".."}


def test_common_layer_does_not_import_engine_or_server() -> None:
    imports = [
        item
        for item in _python_imports(REPO_ROOT / "common")
        if item[2].startswith(("engine", "server"))
    ]
    assert not imports, _relative_imports(imports, root=REPO_ROOT)


def test_sandbox_package_does_not_import_safety_or_server() -> None:
    imports = [
        item
        for item in _python_imports(ENGINE_ROOT / "sandbox")
        if item[2].startswith(("engine.safety", "server", "fastapi"))
    ]
    assert not imports, _relative_imports(imports, root=REPO_ROOT)


def test_agents_tools_remain_zero_cross_layer_import() -> None:
    imports = [
        item
        for item in _python_imports(REPO_ROOT / "agents" / "tools")
        if item[2].startswith(("common", "engine", "server"))
    ]
    assert not imports, _relative_imports(imports, root=REPO_ROOT)


def test_python_imports_resolves_relative_cross_package_imports(tmp_path: Path) -> None:
    """L8: Relative imports like ``from ..safety import tool_guard`` must be caught."""
    sandbox_dir = tmp_path / "engine" / "sandbox"
    sandbox_dir.mkdir(parents=True)
    probe = sandbox_dir / "leak.py"
    probe.write_text(
        "from ..safety import tool_guard\nfrom .. import safety\n",
        encoding="utf-8",
    )
    resolved = [mod for _, _, mod in _python_imports(sandbox_dir, repo_root=tmp_path)]
    assert "engine.safety" in resolved
