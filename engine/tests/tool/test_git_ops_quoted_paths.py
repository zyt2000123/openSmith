"""Regression tests for git_ops' sensitive-file scan under C-quoted paths.

``git`` prints a path in C-quoted form — wrapped in double quotes, non-ASCII
bytes rendered as octal escapes — whenever it contains a byte outside the
printable ASCII range, or a ``"``/``\\``.  ``_SENSITIVE_PATTERNS`` anchors on
``$`` or ``($|\\.)``, so the trailing quote made every pattern miss and a
``.env`` under a CJK directory was committed with the guard reporting nothing.

These use a real git repository rather than a monkeypatched ``_run_git``: the
existing coverage hand-fed ASCII strings for both sources, so it could never
observe real git's quoting.  CJK directory names are routine in this repo.
"""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from engine.sandbox import LocalExecutionEnvironment


ROOT = Path(__file__).resolve().parents[3]
SECRET = "SYNTHETIC_TOKEN_VALUE"


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


def _repo(tmp_path: Path, secret_dir: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "app.py").write_text("print('ordinary source')\n")
    secret_parent = repo / secret_dir
    secret_parent.mkdir(parents=True)
    (secret_parent / ".env").write_text(f"SECRET_KEY={SECRET}\n")
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


def _committed_paths(repo: Path) -> str:
    return _git(repo, "show", "--name-only", "--format=", "HEAD").stdout


@pytest.mark.parametrize(
    "secret_dir",
    [
        pytest.param("项目", id="cjk"),
        pytest.param("proj/日本語", id="cjk-nested"),
        pytest.param("plain", id="ascii-control"),
    ],
)
def test_staged_secret_is_refused_whatever_the_path_encoding(
    tmp_path: Path, secret_dir: str
) -> None:
    """A secret already in the index must block the commit, quoted or not."""
    provider = _load_git_ops()
    repo = _repo(tmp_path, secret_dir)
    _git(repo, "add", "-A")

    result = _commit(provider, repo)

    assert "refusing to stage sensitive files" in result, (
        f"guard let a staged secret through for {secret_dir!r}: {result}"
    )
    assert _git(repo, "rev-parse", "HEAD").returncode != 0, "a commit was created anyway"


def test_ordinary_commit_still_works(tmp_path: Path, monkeypatch) -> None:
    """The guard must not block a commit that carries no secrets.

    The host's global identity is pinned to a file this test controls: the
    tool resolves ``user.name``/``user.email`` from the host config stack, and
    leaving that to the machine made this test pass or fail with the runner's
    FQDN.
    """
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = Probe User\n\temail = probe@example.invalid\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    provider = _load_git_ops()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "文档.md").write_text("# 普通文档\n")
    (repo / "app.py").write_text("print('hello')\n")
    _git(repo, "add", "-A")

    result = _commit(provider, repo)

    assert "refusing" not in result, result
    assert "app.py" in _committed_paths(repo)


def test_scan_fails_closed_when_git_errors(tmp_path: Path, monkeypatch) -> None:
    """A failed index scan must refuse, not silently yield an empty file list."""
    provider = _load_git_ops()
    repo = _repo(tmp_path, "项目")
    _git(repo, "add", "-A")

    original = provider._run_git

    async def failing(args, **kwargs):
        if args[:2] == ["diff", "--name-only"] or "--cached" in args:
            return (128, "", "fatal: simulated index read failure")
        return await original(args, **kwargs)

    monkeypatch.setattr(provider, "_run_git", failing)

    result = _commit(provider, repo)

    # The provider surfaces a failed plumbing call as its exit_code envelope.
    assert "exit_code=128" in result, (
        f"a failed index scan must not fall through to commit: {result}"
    )
    assert _git(repo, "rev-parse", "HEAD").returncode != 0, "a commit was created anyway"
