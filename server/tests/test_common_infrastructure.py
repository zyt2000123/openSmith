from __future__ import annotations

import asyncio
import stat
import sys
from pathlib import Path

import pytest
import tomllib
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.config_service import ConfigService  # noqa: E402

from common import database  # noqa: E402
from common import config  # noqa: E402
from common.paths import AppPaths  # noqa: E402
from common.yaml_utils import YamlConfigError, load_yaml, save_yaml  # noqa: E402


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_app_paths_create_private_runtime_dirs_and_exposes_builtin_identities(tmp_path: Path) -> None:
    paths = AppPaths(data_dir=tmp_path / "data", project_root=tmp_path / "project")
    paths.data_dir.mkdir(mode=0o755)
    paths.data_dir.chmod(0o755)

    paths.ensure_base_dirs()

    assert _mode(paths.data_dir) == 0o700
    assert _mode(paths.agent_dir) == 0o700
    assert _mode(paths.sqlite_path.parent) == 0o700
    assert paths.builtin_identities_dir == paths.project_root / "agents" / "identities"


def test_app_paths_honors_explicit_project_root(monkeypatch, tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / "agents").mkdir(parents=True)
    monkeypatch.setenv("AGENT_SMITH_PROJECT_ROOT", str(project_root))

    assert AppPaths.defaults().project_root == project_root.resolve()


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
    config_path.parent.mkdir(mode=0o755)
    config_path.parent.chmod(0o755)
    config_path.write_text("llm: {}\n", encoding="utf-8")
    config_path.chmod(0o644)

    save_yaml(config_path, {"llm": {"model": "test-model"}})

    assert load_yaml(config_path) == {"llm": {"model": "test-model"}}
    assert _mode(config_path.parent) == 0o700
    assert _mode(config_path) == 0o600
    assert list(config_path.parent.glob(".config.yaml.*.tmp")) == []

    config_path.write_text("- not\n- a mapping\n", encoding="utf-8")
    with pytest.raises(YamlConfigError, match="mapping"):
        load_yaml(config_path)


def test_yaml_surfaces_invalid_documents_and_unsafe_values(tmp_path: Path) -> None:
    invalid_path = tmp_path / "invalid.yaml"
    invalid_path.write_text("llm: [unterminated\n", encoding="utf-8")

    with pytest.raises(YamlConfigError, match="Invalid YAML"):
        load_yaml(invalid_path)

    with pytest.raises(YamlConfigError, match="Unable to serialize"):
        save_yaml(tmp_path / "unsafe.yaml", {"path": Path("/tmp/example")})


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
    opened_connections = []
    connect_calls = 0

    async def delayed_connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        await asyncio.sleep(0)
        connection = await real_connect(*args, **kwargs)
        opened_connections.append(connection)
        return connection

    monkeypatch.setattr(database, "_db", None)
    monkeypatch.setattr(database, "SQLITE_PATH", tmp_path / "agent-smith.sqlite")
    monkeypatch.setattr(database, "ensure_dirs", lambda: None)
    monkeypatch.setattr(database.aiosqlite, "connect", delayed_connect)

    async def run() -> None:
        try:
            first, second = await asyncio.gather(database.get_db(), database.get_db())
            assert first is second
            assert connect_calls == 1
        finally:
            monkeypatch.setattr(database, "_db", None)
            for connection in opened_connections:
                await connection.close()

    asyncio.run(run())


def test_get_db_uses_a_lightweight_liveness_probe_for_cached_connections(
    monkeypatch,
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
    monkeypatch.setattr(database, "_db", cached)

    async def run() -> None:
        assert await database.get_db() is cached

    asyncio.run(run())

    assert cached.statements == ["SELECT 1"]
