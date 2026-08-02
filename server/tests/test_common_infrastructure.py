from __future__ import annotations

import asyncio
import importlib
import stat
import sys
from pathlib import Path

import pytest
import tomllib
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.config_service import ConfigService  # noqa: E402
from app.services import engine_runtime  # noqa: E402
from app.services import config_service as config_service_module  # noqa: E402

from common import database  # noqa: E402
from common import config  # noqa: E402
from common import paths as paths_module  # noqa: E402
from common.paths import AppPaths  # noqa: E402
from common.yaml_utils import YamlConfigError, load_yaml, save_yaml  # noqa: E402


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_app_paths_creates_private_runtime_dirs_without_restricting_existing_parents(
    tmp_path: Path,
) -> None:
    paths = AppPaths(data_dir=tmp_path / "data", project_root=tmp_path / "project")
    paths.data_dir.mkdir(mode=0o755)
    paths.data_dir.chmod(0o755)

    paths.ensure_base_dirs()

    assert _mode(paths.data_dir) == 0o755
    assert _mode(paths.agent_dir) == 0o700
    assert _mode(paths.sqlite_path.parent) == 0o700
    assert paths.builtin_identities_dir == paths.project_root / "agents" / "identities"


def test_app_paths_reports_a_file_conflicting_with_the_runtime_data_directory(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "data"
    data_path.write_text("not a directory", encoding="utf-8")

    with pytest.raises(NotADirectoryError, match="Private runtime path"):
        AppPaths(data_dir=data_path, project_root=tmp_path / "project").ensure_base_dirs()


def test_app_paths_honors_explicit_project_root(monkeypatch, tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / "agents").mkdir(parents=True)
    monkeypatch.setenv("AGENT_SMITH_PROJECT_ROOT", str(project_root))

    assert AppPaths.defaults().project_root == project_root.resolve()


def test_app_paths_cwd_discovery_skips_unrelated_agents_directories(
    monkeypatch, tmp_path: Path
) -> None:
    package_path = tmp_path / "site-packages" / "common" / "paths.py"
    package_path.parent.mkdir(parents=True)
    monkeypatch.setattr(paths_module, "__file__", str(package_path))

    smith_root = tmp_path / "smith"
    (smith_root / "agents" / "smith").mkdir(parents=True)
    (smith_root / "agents" / "smith" / "config.yaml").write_text("name: Smith\n")
    (smith_root / "agents" / "identities").mkdir()
    (smith_root / "agents" / "identities" / "smith.yaml").write_text("id: smith\n")
    skill = smith_root / "agents" / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\n---\n")

    unrelated = smith_root / "nested"
    (unrelated / "agents" / "identities").mkdir(parents=True)
    monkeypatch.chdir(unrelated)

    assert AppPaths.defaults().project_root == smith_root


def test_config_exposes_paths_as_a_lazy_app_paths_value(monkeypatch, tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / "agents").mkdir(parents=True)
    monkeypatch.setenv("AGENT_SMITH_PROJECT_ROOT", str(project_root))
    config.reset_paths()

    try:
        from common.config import PATHS

        assert PATHS == AppPaths.defaults()
        assert PATHS.project_root == project_root.resolve()
    finally:
        config.reset_paths()


def test_runtime_catalog_resolves_paths_when_it_runs(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    first_paths = AppPaths(data_dir=tmp_path / "first", project_root=project_root)
    second_paths = AppPaths(data_dir=tmp_path / "second", project_root=project_root)
    config.reset_paths(first_paths)
    reloaded_runtime = importlib.reload(engine_runtime)
    config.reset_paths(second_paths)

    try:
        reloaded_runtime.load_runtime_identity_catalog(force=True)

        assert (second_paths.builtin_skills_dir / "grill-me" / "SKILL.md").is_file()
    finally:
        config.reset_paths()


def test_llm_config_resolves_paths_when_it_runs(tmp_path: Path) -> None:
    from engine.llm import model_config

    project_root = tmp_path / "project"
    smith_config = project_root / "agents" / "smith" / "config.yaml"
    smith_config.parent.mkdir(parents=True)
    smith_config.write_text("llm: {}\n", encoding="utf-8")
    first_paths = AppPaths(data_dir=tmp_path / "first", project_root=project_root)
    second_paths = AppPaths(data_dir=tmp_path / "second", project_root=project_root)
    (first_paths.data_dir).mkdir(parents=True)
    (first_paths.data_dir / "config.yaml").write_text(
        "llm:\n  pricing:\n    first:\n      input: 1\n", encoding="utf-8"
    )
    (second_paths.data_dir).mkdir(parents=True)
    (second_paths.data_dir / "config.yaml").write_text(
        "llm:\n  pricing:\n    second:\n      input: 2\n", encoding="utf-8"
    )

    config.reset_paths(first_paths)
    reloaded_model_config = importlib.reload(model_config)
    config.reset_paths(second_paths)

    try:
        assert reloaded_model_config.resolve_price_table() == {"second": {"input": 2.0}}
    finally:
        config.reset_paths()


def test_config_service_resolves_paths_when_it_runs(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    first_paths = AppPaths(data_dir=tmp_path / "first", project_root=project_root)
    second_paths = AppPaths(data_dir=tmp_path / "second", project_root=project_root)
    first_paths.data_dir.mkdir(parents=True)
    (first_paths.data_dir / "config.yaml").write_text(
        "llm:\n  model: first\n", encoding="utf-8"
    )
    second_paths.data_dir.mkdir(parents=True)
    (second_paths.data_dir / "config.yaml").write_text(
        "llm:\n  model: second\n", encoding="utf-8"
    )

    config.reset_paths(first_paths)
    reloaded_service = importlib.reload(config_service_module)
    config.reset_paths(second_paths)

    try:
        assert reloaded_service.ConfigService()._load_config() == {"llm": {"model": "second"}}
    finally:
        config.reset_paths()


def test_app_paths_installs_shipped_skills_separately_from_user_skills(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    paths = AppPaths(data_dir=tmp_path / "data", project_root=project_root)

    paths.ensure_base_dirs()

    shipped = sorted(
        child.name
        for child in paths.bundled_skills_dir.iterdir()
        if (child / "SKILL.md").is_file()
    )
    installed = sorted(child.name for child in paths.builtin_skills_dir.iterdir() if child.is_dir())

    assert paths.builtin_skills_dir == paths.data_dir / "builtin" / "skills"
    # Every shipped skill (a directory holding a SKILL.md entry file) is
    # mirrored, and nothing else is left in the Smith-owned directory.
    assert shipped, "no shipped skill was discovered"
    assert installed == shipped
    assert not (paths.agent_dir / "skills").exists()


def test_app_paths_reconciles_existing_builtin_skill_directory(
    monkeypatch, tmp_path: Path
) -> None:
    project_root = tmp_path / "project"
    source_skill = project_root / "agents" / "skills" / "demo"
    source_skill.mkdir(parents=True)
    (source_skill / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    obsolete_source_file = source_skill / "obsolete.md"
    obsolete_source_file.write_text("old instruction", encoding="utf-8")
    monkeypatch.setattr(
        "common.paths.sysconfig.get_path", lambda _name: str(tmp_path / "no-data")
    )

    paths = AppPaths(data_dir=tmp_path / "data", project_root=project_root)
    paths.ensure_base_dirs()
    obsolete_target_file = paths.builtin_skills_dir / "demo" / "obsolete.md"
    assert obsolete_target_file.is_file()

    obsolete_source_file.unlink()
    paths.ensure_base_dirs()

    assert not obsolete_target_file.exists()


def test_app_paths_keeps_installed_skills_when_the_shipped_source_is_empty(
    monkeypatch, tmp_path: Path
) -> None:
    package_data = tmp_path / "package-data"
    (package_data / "agent_smith_common" / "builtin_skills").mkdir(parents=True)
    monkeypatch.setattr(
        "common.paths.sysconfig.get_path", lambda _name: str(package_data)
    )

    paths = AppPaths(data_dir=tmp_path / "data", project_root=tmp_path / "project")
    installed_skill = paths.builtin_skills_dir / "existing" / "SKILL.md"
    installed_skill.parent.mkdir(parents=True)
    installed_skill.write_text("---\nname: existing\n---\n", encoding="utf-8")

    paths.ensure_base_dirs()

    assert installed_skill.is_file()


def test_app_paths_rejects_symlinked_builtin_skill_target(
    monkeypatch, tmp_path: Path
) -> None:
    project_root = tmp_path / "project"
    source_skill = project_root / "agents" / "skills" / "demo"
    source_skill.mkdir(parents=True)
    (source_skill / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    monkeypatch.setattr(
        "common.paths.sysconfig.get_path", lambda _name: str(tmp_path / "no-data")
    )

    data_dir = tmp_path / "data"
    target_root = data_dir / "builtin" / "skills"
    target_root.mkdir(parents=True)
    outside_target = tmp_path / "outside"
    outside_target.mkdir()
    (target_root / "demo").symlink_to(outside_target, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlink"):
        AppPaths(data_dir=data_dir, project_root=project_root).ensure_base_dirs()

    assert not (outside_target / "SKILL.md").exists()


def test_app_paths_rejects_a_symlinked_builtin_skill_manifest(
    monkeypatch, tmp_path: Path
) -> None:
    project_root = tmp_path / "project"
    source_skill = project_root / "agents" / "skills" / "demo"
    source_skill.mkdir(parents=True)
    (source_skill / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    monkeypatch.setattr(
        "common.paths.sysconfig.get_path", lambda _name: str(tmp_path / "no-data")
    )

    paths = AppPaths(data_dir=tmp_path / "data", project_root=project_root)
    manifest_path = paths.builtin_skills_dir / ".manifest.json"
    manifest_path.parent.mkdir(parents=True)
    outside_manifest = tmp_path / "outside.json"
    outside_manifest.write_text('{"files": {}}', encoding="utf-8")
    manifest_path.symlink_to(outside_manifest)

    with pytest.raises(RuntimeError, match="symlink"):
        paths.ensure_base_dirs()

    assert outside_manifest.read_text(encoding="utf-8") == '{"files": {}}'


def test_app_paths_recovers_from_an_invalid_builtin_skill_manifest(
    monkeypatch, tmp_path: Path
) -> None:
    project_root = tmp_path / "project"
    source_skill = project_root / "agents" / "skills" / "demo"
    source_skill.mkdir(parents=True)
    (source_skill / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    monkeypatch.setattr(
        "common.paths.sysconfig.get_path", lambda _name: str(tmp_path / "no-data")
    )

    paths = AppPaths(data_dir=tmp_path / "data", project_root=project_root)
    manifest_path = paths.builtin_skills_dir / ".manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("[]", encoding="utf-8")

    paths.ensure_base_dirs()

    assert (paths.builtin_skills_dir / "demo" / "SKILL.md").is_file()


def test_app_paths_replaces_a_directory_that_conflicts_with_a_shipped_file(
    monkeypatch, tmp_path: Path
) -> None:
    project_root = tmp_path / "project"
    source_skill = project_root / "agents" / "skills" / "demo"
    source_skill.mkdir(parents=True)
    (source_skill / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    (source_skill / "instruction.md").write_text("source", encoding="utf-8")
    monkeypatch.setattr(
        "common.paths.sysconfig.get_path", lambda _name: str(tmp_path / "no-data")
    )

    paths = AppPaths(data_dir=tmp_path / "data", project_root=project_root)
    conflicting_target = paths.builtin_skills_dir / "demo" / "instruction.md"
    conflicting_target.mkdir(parents=True)

    paths.ensure_base_dirs()

    assert conflicting_target.read_text(encoding="utf-8") == "source"


def test_app_paths_replaces_a_file_that_conflicts_with_a_shipped_directory(
    monkeypatch, tmp_path: Path
) -> None:
    project_root = tmp_path / "project"
    source_skill = project_root / "agents" / "skills" / "demo"
    (source_skill / "references").mkdir(parents=True)
    (source_skill / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    (source_skill / "references" / "guide.md").write_text("source", encoding="utf-8")
    monkeypatch.setattr(
        "common.paths.sysconfig.get_path", lambda _name: str(tmp_path / "no-data")
    )

    paths = AppPaths(data_dir=tmp_path / "data", project_root=project_root)
    conflicting_target = paths.builtin_skills_dir / "demo" / "references"
    conflicting_target.parent.mkdir(parents=True)
    conflicting_target.write_text("not a directory", encoding="utf-8")

    paths.ensure_base_dirs()

    assert (conflicting_target / "guide.md").read_text(encoding="utf-8") == "source"


def test_app_paths_restores_tampered_shipped_skill_files(
    monkeypatch, tmp_path: Path
) -> None:
    project_root = tmp_path / "project"
    source_skill = project_root / "agents" / "skills" / "demo"
    source_skill.mkdir(parents=True)
    (source_skill / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    (source_skill / "instruction.md").write_text("trusted source", encoding="utf-8")
    monkeypatch.setattr(
        "common.paths.sysconfig.get_path", lambda _name: str(tmp_path / "no-data")
    )

    paths = AppPaths(data_dir=tmp_path / "data", project_root=project_root)
    paths.ensure_base_dirs()
    installed_file = paths.builtin_skills_dir / "demo" / "instruction.md"
    installed_file.write_text("tampered", encoding="utf-8")

    paths.ensure_base_dirs()

    assert installed_file.read_text(encoding="utf-8") == "trusted source"


def test_wheel_data_files_reproduce_every_bundled_skill_file() -> None:
    """A non-editable install reproduces every file from each shipped skill.

    ``bundled_skills_dir`` prefers the wheel's data-files directory.  The
    manifest must therefore preserve both every file and its relative directory
    inside a skill, not merely the top-level ``SKILL.md`` entry file.
    """
    repo_root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads(
        (repo_root / "common" / "pyproject.toml").read_text(encoding="utf-8")
    )
    skills_root = repo_root / "agents" / "skills"
    prefix = "agent_smith_common/builtin_skills/"
    declared = {
        target: set(files)
        for target, files in pyproject["tool"]["setuptools"]["data-files"].items()
        if target.startswith(prefix)
    }
    shipped: dict[str, set[str]] = {}
    for skill_dir in skills_root.iterdir():
        if not (skill_dir / "SKILL.md").is_file():
            continue
        for source_file in skill_dir.rglob("*"):
            if not source_file.is_file():
                continue
            relative = source_file.relative_to(skills_root)
            target = f"{prefix}{relative.parent.as_posix()}"
            shipped.setdefault(target, set()).add(f"../agents/skills/{relative.as_posix()}")

    assert shipped, "no shipped skill was discovered"
    assert declared == shipped


def test_yaml_requires_a_mapping_and_preserves_private_atomic_file(tmp_path: Path) -> None:
    config_path = tmp_path / "private" / "config.yaml"

    save_yaml(config_path, {"llm": {"model": "test-model"}})

    assert load_yaml(config_path) == {"llm": {"model": "test-model"}}
    assert _mode(config_path.parent) == 0o700
    assert _mode(config_path) == 0o600
    assert list(config_path.parent.glob(".config.yaml.*.tmp")) == []

    config_path.write_text("- not\n- a mapping\n", encoding="utf-8")
    with pytest.raises(YamlConfigError, match="mapping"):
        load_yaml(config_path)


def test_yaml_save_preserves_existing_parent_permissions(monkeypatch, tmp_path: Path) -> None:
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir(mode=0o755)
    shared_dir.chmod(0o755)

    save_yaml(shared_dir / "config.yaml", {"llm": {}})

    assert _mode(shared_dir) == 0o755

    monkeypatch.chdir(shared_dir)
    save_yaml("relative.yaml", {"llm": {}})

    assert _mode(shared_dir) == 0o755


def test_yaml_surfaces_invalid_documents_and_unsafe_values(tmp_path: Path) -> None:
    invalid_path = tmp_path / "invalid.yaml"
    invalid_path.write_text("llm: [unterminated\n", encoding="utf-8")

    with pytest.raises(YamlConfigError, match="Invalid YAML"):
        load_yaml(invalid_path)

    with pytest.raises(YamlConfigError, match="Unable to serialize"):
        save_yaml(tmp_path / "unsafe.yaml", {"path": Path("/tmp/example")})


def test_yaml_save_rejects_a_non_mapping_document(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"

    with pytest.raises(YamlConfigError, match="mapping"):
        save_yaml(config_path, ["not", "a", "config"])

    assert not config_path.exists()


def test_config_service_returns_422_for_an_invalid_config(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("- invalid root\n", encoding="utf-8")
    monkeypatch.setattr(ConfigService, "_config_path", config_path)

    with pytest.raises(HTTPException) as exc:
        ConfigService().get_llm_config()

    assert exc.value.status_code == 422
    assert "mapping" in exc.value.detail

    config_path.write_text("llm: not-a-mapping\n", encoding="utf-8")
    with pytest.raises(HTTPException) as exc:
        ConfigService().get_llm_config()

    assert exc.value.status_code == 422
    assert "llm" in exc.value.detail


def test_get_db_initializes_once_for_concurrent_callers(monkeypatch, tmp_path: Path) -> None:
    real_connect = database.aiosqlite.connect
    connect_calls = 0

    async def delayed_connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        await asyncio.sleep(0)
        return await real_connect(*args, **kwargs)

    paths = AppPaths(data_dir=tmp_path / "data", project_root=tmp_path / "project")
    config.reset_paths(paths)
    monkeypatch.setattr(database.aiosqlite, "connect", delayed_connect)

    async def run() -> None:
        try:
            await database.close_db()
            first, second = await asyncio.gather(database.get_db(), database.get_db())
            assert first is second
            assert connect_calls == 1
        finally:
            await database.close_db()
            config.reset_paths()

    asyncio.run(run())


def test_get_db_reconnects_when_runtime_paths_change(tmp_path: Path) -> None:
    first_paths = AppPaths(data_dir=tmp_path / "first", project_root=tmp_path / "project")
    second_paths = AppPaths(data_dir=tmp_path / "second", project_root=tmp_path / "project")
    config.reset_paths(first_paths)
    reloaded_database = importlib.reload(database)

    async def run() -> None:
        try:
            first_connection = await reloaded_database.get_db()
            config.reset_paths(second_paths)
            second_connection = await reloaded_database.get_db()

            assert second_connection is not first_connection
            assert second_paths.sqlite_path.is_file()
        finally:
            await reloaded_database.close_db()
            config.reset_paths()

    asyncio.run(run())


def test_get_db_returns_cached_connections_without_an_sql_round_trip(
    monkeypatch, tmp_path: Path
) -> None:
    class Cursor:
        async def close(self) -> None:
            return None

    class CachedConnection:
        def __init__(self) -> None:
            self.statements: list[str] = []

        async def execute(self, statement: str) -> Cursor:
            self.statements.append(statement)
            return Cursor()

    cached = CachedConnection()
    paths = AppPaths(data_dir=tmp_path / "data", project_root=tmp_path / "project")
    config.reset_paths(paths)
    monkeypatch.setattr(database, "_db", cached)
    monkeypatch.setattr(database, "_db_path", paths.sqlite_path)

    async def run() -> None:
        assert await database.get_db() is cached

    try:
        asyncio.run(run())
    finally:
        config.reset_paths()

    assert cached.statements == []
