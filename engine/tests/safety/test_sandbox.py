"""Contract tests for the engine-level sandbox execution boundary."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from engine.sandbox import (
    CommandResult,
    ExecutionEnvironment,
    LocalExecutionEnvironment,
    MacOSSeatbeltEnvironment,
)


def test_host_environment_remains_an_execution_environment() -> None:
    """Host execution remains a supported explicit backend."""
    assert isinstance(LocalExecutionEnvironment(), ExecutionEnvironment)


def test_macos_seatbelt_environment_is_a_sandbox_backend(tmp_path: Path) -> None:
    environment = MacOSSeatbeltEnvironment(workspace=tmp_path)

    assert isinstance(environment, ExecutionEnvironment)
    assert environment.name == "sandbox"
    assert "(deny network*)" in environment._profile()


def test_macos_seatbelt_rejects_workspace_paths_with_control_characters(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace\n(allow default)"
    workspace.mkdir()

    with pytest.raises(ValueError, match="control characters"):
        MacOSSeatbeltEnvironment(workspace=workspace)


@pytest.mark.parametrize(
    "relative_workspace",
    [
        ".ssh",
        ".git",
        ".agent-smith",
        ".config/gh",
        "Library/Keychains",
    ],
)
def test_macos_seatbelt_rejects_a_protected_directory_as_the_workspace(
    tmp_path: Path,
    relative_workspace: str,
) -> None:
    workspace = tmp_path / relative_workspace
    workspace.mkdir(parents=True)

    with pytest.raises(ValueError, match="protected directory"):
        MacOSSeatbeltEnvironment(workspace=workspace)


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_macos_seatbelt_handles_workspace_paths_with_profile_metacharacters(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / 'project [safe] "quoted"'
    workspace.mkdir()
    (workspace / "README.md").write_text("safe\n", encoding="utf-8")

    result = asyncio.run(
        MacOSSeatbeltEnvironment(workspace=workspace).run_command(
            argv=["/bin/cat", "README.md"],
            cwd=str(workspace),
        )
    )

    assert result.exit_code == 0, result.stderr or result.error
    assert result.stdout == "safe\n"


def test_macos_seatbelt_rejects_a_working_directory_outside_the_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = asyncio.run(
        MacOSSeatbeltEnvironment(workspace=workspace).run_command(
            argv=["/bin/echo", "never-runs"], cwd=str(tmp_path)
        )
    )

    assert result.exit_code is None
    assert result.error and "escapes workspace" in result.error


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_macos_seatbelt_does_not_inherit_parent_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SMITH_TEST_SECRET", "must-not-leak")
    environment = MacOSSeatbeltEnvironment(workspace=tmp_path)

    result = asyncio.run(
        environment.run_command(argv=["/usr/bin/env"], cwd=str(tmp_path))
    )

    assert result.exit_code == 0, result.stderr or result.error
    assert "SMITH_TEST_SECRET" not in result.stdout
    assert f"HOME={tmp_path.resolve()}" in result.stdout
    assert "GIT_CONFIG_GLOBAL=/dev/null" in result.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_macos_seatbelt_runs_in_workspace_and_blocks_writes_outside_it(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected_path = tmp_path / "outside.txt"
    environment = MacOSSeatbeltEnvironment(workspace=workspace)

    allowed = asyncio.run(
        environment.run_command(
            argv=["/bin/sh", "-c", "printf allowed > inside.txt"],
            cwd=str(workspace),
        )
    )
    blocked = asyncio.run(
        environment.run_command(
            argv=["/bin/sh", "-c", f"printf blocked > {protected_path}"],
            cwd=str(workspace),
        )
    )

    assert allowed == CommandResult(exit_code=0)
    assert (workspace / "inside.txt").read_text() == "allowed"
    assert blocked.exit_code != 0
    assert not protected_path.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_macos_seatbelt_blocks_dynamic_secret_reads_inside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("SMITH_SECRET=must-not-leak\n", encoding="utf-8")
    environment = MacOSSeatbeltEnvironment(workspace=workspace)

    result = asyncio.run(
        environment.run_command(
            command="/bin/cat ./.en?",
            cwd=str(workspace),
        )
    )

    assert result.exit_code not in (None, 0)
    assert "must-not-leak" not in result.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_macos_seatbelt_blocks_nested_env_variants_but_allows_normal_files(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "config"
    nested.mkdir(parents=True)
    (nested / ".env.local").write_text("TOKEN=must-not-leak\n", encoding="utf-8")
    (nested / "README.md").write_text("safe project documentation\n", encoding="utf-8")
    environment = MacOSSeatbeltEnvironment(workspace=workspace)

    blocked = asyncio.run(
        environment.run_command(
            command='dir=config; suffix=local; /bin/cat "$dir/.env.$suffix"',
            cwd=str(workspace),
        )
    )
    allowed = asyncio.run(
        environment.run_command(
            argv=["/bin/cat", "config/README.md"],
            cwd=str(workspace),
        )
    )

    assert blocked.exit_code not in (None, 0)
    assert "must-not-leak" not in blocked.stdout
    assert allowed.exit_code == 0, allowed.stderr or allowed.error
    assert allowed.stdout == "safe project documentation\n"


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
@pytest.mark.parametrize(
    "relative_path",
    [
        ".ssh/id_ed25519",
        "nested/.aws/credentials",
        "nested/.gnupg/private-keys-v1.d/key",
        "nested/.kube/config",
        "certs/client.pem",
        "certs/client.key",
        "certs/client.p12",
        "certs/client.pfx",
        ".ENV",
        "nested/.SSH/id_ed25519",
        "certs/client.PEM",
        ".agent-smith/config.yaml",
        ".docker/config.json",
        ".config/gh/hosts.yml",
        ".config/gcloud/credentials.db",
        "Library/Keychains/login.keychain-db",
        "keys/id_ed25519",
    ],
)
def test_macos_seatbelt_blocks_workspace_credentials_and_private_keys(
    tmp_path: Path,
    relative_path: str,
) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("must-not-leak\n", encoding="utf-8")
    environment = MacOSSeatbeltEnvironment(workspace=workspace)

    result = asyncio.run(
        environment.run_command(
            argv=["/bin/cat", relative_path],
            cwd=str(workspace),
        )
    )

    assert result.exit_code not in (None, 0)
    assert "must-not-leak" not in result.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
@pytest.mark.parametrize(
    "relative_path",
    [
        ".git/config",
        ".env",
        "config/.env.example",
        ".npmrc",
        "config/.pypirc",
        ".GIT/config",
        "config/.NPMRC",
    ],
)
def test_macos_seatbelt_blocks_sensitive_writes_inside_workspace(
    tmp_path: Path,
    relative_path: str,
) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("original\n", encoding="utf-8")
    environment = MacOSSeatbeltEnvironment(workspace=workspace)

    blocked = asyncio.run(
        environment.run_command(
            argv=[
                "/bin/sh",
                "-c",
                'printf compromised > "$1"',
                "sandbox-test",
                relative_path,
            ],
            cwd=str(workspace),
        )
    )
    allowed = asyncio.run(
        environment.run_command(
            argv=[
                "/bin/sh",
                "-c",
                "printf allowed > normal.txt",
            ],
            cwd=str(workspace),
        )
    )

    assert blocked.exit_code not in (None, 0)
    assert target.read_text(encoding="utf-8") == "original\n"
    assert allowed.exit_code == 0, allowed.stderr or allowed.error
    assert (workspace / "normal.txt").read_text(encoding="utf-8") == "allowed"


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_macos_seatbelt_allows_reading_git_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    git_config = workspace / ".git" / "config"
    git_config.parent.mkdir(parents=True)
    git_config.write_text("[core]\n", encoding="utf-8")

    result = asyncio.run(
        MacOSSeatbeltEnvironment(workspace=workspace).run_command(
            argv=["/bin/cat", ".git/config"],
            cwd=str(workspace),
        )
    )

    assert result.exit_code == 0, result.stderr or result.error
    assert result.stdout == "[core]\n"


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_macos_seatbelt_blocks_symlink_aliases_to_protected_targets(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_secret = workspace / ".env"
    workspace_secret.write_text("workspace-secret\n", encoding="utf-8")
    outside_secret = tmp_path / "outside-secret.txt"
    outside_secret.write_text("outside-secret\n", encoding="utf-8")
    (workspace / "workspace-alias.txt").symlink_to(workspace_secret)
    (workspace / "outside-alias.txt").symlink_to(outside_secret)
    environment = MacOSSeatbeltEnvironment(workspace=workspace)

    workspace_alias = asyncio.run(
        environment.run_command(
            argv=["/bin/cat", "workspace-alias.txt"],
            cwd=str(workspace),
        )
    )
    outside_alias = asyncio.run(
        environment.run_command(
            argv=["/bin/cat", "outside-alias.txt"],
            cwd=str(workspace),
        )
    )

    assert workspace_alias.exit_code not in (None, 0)
    assert "workspace-secret" not in workspace_alias.stdout
    assert outside_alias.exit_code not in (None, 0)
    assert "outside-secret" not in outside_alias.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_macos_seatbelt_fails_closed_on_a_hardlink_alias_to_a_secret(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_secret = workspace / ".env"
    workspace_secret.write_text("hardlink-secret\n", encoding="utf-8")
    os.link(workspace_secret, workspace / "apparently-safe.txt")
    environment = MacOSSeatbeltEnvironment(workspace=workspace)

    result = asyncio.run(
        environment.run_command(
            argv=["/bin/cat", "apparently-safe.txt"],
            cwd=str(workspace),
        )
    )

    assert result.exit_code is None
    assert result.error and "hard links" in result.error
    assert "hardlink-secret" not in result.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_macos_seatbelt_fails_closed_on_a_hardlink_from_outside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_secret = tmp_path / "outside-secret.txt"
    outside_secret.write_text("outside-hardlink-secret\n", encoding="utf-8")
    os.link(outside_secret, workspace / "apparently-safe.txt")
    environment = MacOSSeatbeltEnvironment(workspace=workspace)

    result = asyncio.run(
        environment.run_command(
            argv=["/bin/cat", "apparently-safe.txt"],
            cwd=str(workspace),
        )
    )

    assert result.exit_code is None
    assert result.error and "hard links" in result.error
    assert "outside-hardlink-secret" not in result.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_macos_seatbelt_fails_closed_on_a_hardlink_alias_to_git_metadata(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    git_config = workspace / ".git" / "config"
    git_config.parent.mkdir(parents=True)
    git_config.write_text("original\n", encoding="utf-8")
    alias = workspace / "apparently-safe.txt"
    os.link(git_config, alias)
    environment = MacOSSeatbeltEnvironment(workspace=workspace)

    result = asyncio.run(
        environment.run_command(
            argv=[
                "/bin/sh",
                "-c",
                'printf compromised > "$1"',
                "sandbox-test",
                alias.name,
            ],
            cwd=str(workspace),
        )
    )

    assert result.exit_code is None
    assert result.error and "hard links" in result.error
    assert git_config.read_text(encoding="utf-8") == "original\n"


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
@pytest.mark.parametrize("relative_path", [".env", ".git/config"])
def test_macos_seatbelt_blocks_creating_hardlink_aliases_during_execution(
    tmp_path: Path,
    relative_path: str,
) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("original\n", encoding="utf-8")
    alias = workspace / "new-alias.txt"
    environment = MacOSSeatbeltEnvironment(workspace=workspace)

    result = asyncio.run(
        environment.run_command(
            argv=[
                "/bin/sh",
                "-c",
                '/bin/ln "$1" new-alias.txt && printf compromised > new-alias.txt',
                "sandbox-test",
                relative_path,
            ],
            cwd=str(workspace),
        )
    )

    assert result.exit_code not in (None, 0)
    assert not alias.exists()
    assert target.read_text(encoding="utf-8") == "original\n"


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_macos_seatbelt_blocks_outbound_network(tmp_path: Path) -> None:
    result = asyncio.run(
        MacOSSeatbeltEnvironment(workspace=tmp_path).run_command(
            argv=["/usr/bin/curl", "--connect-timeout", "1", "https://example.com"],
            cwd=str(tmp_path),
            timeout_seconds=3,
        )
    )

    assert result.exit_code not in (None, 0)
    assert "Operation not permitted" in result.stderr
