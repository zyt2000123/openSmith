"""Regression tests for commit identity under the isolated git environment.

``_safe_environment`` hides the global/system git config from every git
subprocess so repository-controlled hooks and helpers cannot read runtime
secrets.  Until the identity fix that also hid ``user.name``/``user.email``:
committing in a repo with no local identity failed outright on hosts without
a valid FQDN — and on hosts *with* one, git silently fabricated ``user@fqdn``
and committed with an identity the user never configured.

The contract pinned here: identity comes from the user's real git config
(repo-local beating global, matching git's own precedence), and when nothing
is configured the commit fails with git's actionable message instead of
inventing an author.
"""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
from pathlib import Path

from engine.sandbox import LocalExecutionEnvironment

ROOT = Path(__file__).resolve().parents[3]


def _load_git_ops():
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module("agents.tools.git_ops")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=probe", "-c", "user.email=probe@example.invalid", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def _fresh_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "app.py").write_text("print('ordinary source')\n")
    _git(repo, "add", "-A")
    return repo


def _commit(provider, repo: Path) -> str:
    return asyncio.run(
        provider.execute(
            action="commit",
            message="probe commit",
            cwd=str(repo),
            environment=LocalExecutionEnvironment(),
        )
    )


def _head_author(repo: Path) -> str:
    return _git(repo, "show", "-s", "--format=%an <%ae>", "HEAD").stdout.strip()


def _write_global_config(tmp_path: Path, monkeypatch, name: str, email: str) -> None:
    config = tmp_path / "gitconfig"
    config.write_text(f"[user]\n\tname = {name}\n\temail = {email}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))


def test_commit_uses_the_hosts_global_identity(tmp_path: Path, monkeypatch) -> None:
    _write_global_config(tmp_path, monkeypatch, "Global User", "global@example.invalid")
    provider = _load_git_ops()
    repo = _fresh_repo(tmp_path)

    result = _commit(provider, repo)

    assert "[exit_code=0]" in result, result
    assert _head_author(repo) == "Global User <global@example.invalid>"


def test_repo_local_identity_beats_the_global_one(tmp_path: Path, monkeypatch) -> None:
    _write_global_config(tmp_path, monkeypatch, "Global User", "global@example.invalid")
    provider = _load_git_ops()
    repo = _fresh_repo(tmp_path)
    _git(repo, "config", "user.name", "Local User")
    _git(repo, "config", "user.email", "local@example.invalid")

    result = _commit(provider, repo)

    assert "[exit_code=0]" in result, result
    assert _head_author(repo) == "Local User <local@example.invalid>"


def test_commit_without_any_identity_fails_instead_of_fabricating_one(
    tmp_path: Path, monkeypatch
) -> None:
    """No configured identity must mean a clear failure, never ``user@fqdn``."""
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    bare_home = tmp_path / "bare-home"
    bare_home.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("HOME", str(bare_home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    provider = _load_git_ops()
    repo = _fresh_repo(tmp_path)

    result = _commit(provider, repo)

    assert "[exit_code=0]" not in result, result
    assert _git(repo, "rev-parse", "HEAD").returncode != 0, "no commit must be created"
