from __future__ import annotations

import asyncio
import stat
import sys
import tomllib
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.config_service import ConfigService  # noqa: E402
from common import database  # noqa: E402
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


def test_wheel_data_files_declare_every_bundled_skill() -> None:
    """A non-editable install ships skills through ``data-files``.

    ``bundled_skills_dir`` prefers that installed copy, so a skill missing from
    the declaration is absent from a wheel install while still working from a
    source checkout. setuptools needs one entry per skill (a glob would flatten
    every ``SKILL.md`` into a single directory), so assert the two stay in sync.
    """
    repo_root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads(
        (repo_root / "common" / "pyproject.toml").read_text(encoding="utf-8")
    )
    prefix = "agent_smith_common/builtin_skills/"
    declared = {
        target[len(prefix) :]
        for target in pyproject["tool"]["setuptools"]["data-files"]
        if target.startswith(prefix)
    }
    shipped = {
        child.name
        for child in (repo_root / "agents" / "skills").iterdir()
        if (child / "SKILL.md").is_file()
    }

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
