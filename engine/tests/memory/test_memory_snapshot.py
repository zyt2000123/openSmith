"""Snapshots must recover a document that two later writes overwrote."""

from __future__ import annotations

import subprocess

import pytest

from engine.memory._snapshot import TRACKED_VIEWS, snapshot_views


def _agent_root(tmp_path):
    root = tmp_path / "agent"
    (root / "memory").mkdir(parents=True)
    return root


def _write_views(root, *, context: str, durable: str) -> None:
    (root / "context.md").write_text(context, encoding="utf-8")
    (root / "memory" / "durable.md").write_text(durable, encoding="utf-8")


def _show(root, ref: str, path: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), "show", f"{ref}:{path}"),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_two_bad_writes_still_leave_the_good_version_recoverable(tmp_path):
    """The failure `.bak` could not survive: it holds one generation only."""
    root = _agent_root(tmp_path)

    _write_views(root, context="# Smith Context\n", durable="# good\n")
    assert snapshot_views(root, "memory: durable (written)") is True

    _write_views(root, context="# Smith Context\n", durable="# bad\n")
    assert snapshot_views(root, "memory: durable (written)") is True

    _write_views(root, context="# Smith Context\n", durable="# worse\n")
    assert snapshot_views(root, "memory: durable (written)") is True

    assert _show(root, "HEAD~2", "memory/durable.md") == "# good\n"


def test_unchanged_views_are_a_noop_not_a_failure(tmp_path):
    root = _agent_root(tmp_path)
    _write_views(root, context="# Smith Context\n", durable="# same\n")

    assert snapshot_views(root, "first") is True
    # Compilation rewrites the file on every accepted draft, including drafts
    # that reproduce the previous document byte for byte.
    assert snapshot_views(root, "second") is False


def test_only_the_two_views_ever_enter_the_repository(tmp_path):
    """A whitelist, never `git add -A`: the agent root also holds secrets."""
    root = _agent_root(tmp_path)
    _write_views(root, context="# Smith Context\n", durable="# durable\n")
    (root / "config.yaml").write_text("api_key: sk-not-a-real-key\n", encoding="utf-8")
    (root / "auth_token").write_text("token\n", encoding="utf-8")

    assert snapshot_views(root, "memory: durable (written)") is True

    tracked = subprocess.run(
        ("git", "-C", str(root), "ls-files"),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert sorted(tracked) == sorted(TRACKED_VIEWS)


def test_snapshot_failure_never_raises(tmp_path):
    """Memory must keep working when the snapshot cannot be taken."""
    missing = tmp_path / "does-not-exist"
    assert snapshot_views(missing, "memory: durable (written)") is False


def test_missing_views_are_skipped_rather_than_committed(tmp_path):
    root = _agent_root(tmp_path)
    # A fresh install before the first compilation: no rendered view exists yet.
    assert snapshot_views(root, "memory: durable (written)") is False


def test_git_absent_is_a_warning_not_an_error(tmp_path, monkeypatch):
    root = _agent_root(tmp_path)
    _write_views(root, context="# Smith Context\n", durable="# durable\n")

    def _no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _no_git)
    assert snapshot_views(root, "memory: durable (written)") is False


def test_timeout_is_swallowed(tmp_path, monkeypatch):
    root = _agent_root(tmp_path)
    _write_views(root, context="# Smith Context\n", durable="# durable\n")

    def _hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=15.0)

    monkeypatch.setattr(subprocess, "run", _hang)
    assert snapshot_views(root, "memory: durable (written)") is False


def test_dream_sanitize_leaves_a_trace_and_a_snapshot(tmp_path):
    """Sanitizing deletes without evidence; without a trace it is indistinguishable
    from a compiler deletion, and a false-positive secret match loses real memory
    silently."""
    from engine.memory.dream import DreamReport, _sanitize_all_layers

    root = _agent_root(tmp_path)
    memory_dir = root / "memory"
    (root / "context.md").write_text("# Smith Context\n", encoding="utf-8")
    (memory_dir / "durable.md").write_text(
        "# Durable Project Memory\n\n## Decisions\n"
        "- **部署**: 决定 token 是 ghp_0123456789abcdefghijABCDEFGHIJ0123；适用范围：全局。\n",
        encoding="utf-8",
    )

    report = DreamReport()
    _sanitize_all_layers(memory_dir, report)

    assert report.secrets_removed > 0
    surviving = (memory_dir / "durable.md").read_text(encoding="utf-8")
    assert "ghp_0123456789abcdefghijABCDEFGHIJ0123" not in surviving

    history = (memory_dir / "memory_history.jsonl").read_text(encoding="utf-8")
    assert '"status": "sanitized"' in history
    assert '"target": "durable.md"' in history

    # The snapshot is taken *after* sanitizing, deliberately: committing the
    # pre-sanitize text would park the leaked secret in git history forever,
    # which is the very thing sanitizing exists to prevent.  So HEAD holds the
    # cleaned document -- the deletion is visible in `git log`, and the secret
    # is not in the repository at all.
    head = _show(root, "HEAD", "memory/durable.md")
    assert "ghp_0123456789abcdefghijABCDEFGHIJ0123" not in head
    assert "Durable Project Memory" in head

    # Recovering from a false-positive match relies on the *compiler's* snapshot
    # instead: _commit_view refuses a draft containing secrets, so whatever
    # compilation committed is clean by construction and still restorable.


@pytest.mark.parametrize("view", TRACKED_VIEWS)
def test_each_view_is_recoverable_independently(tmp_path, view):
    root = _agent_root(tmp_path)
    _write_views(root, context="# ctx v1\n", durable="# dur v1\n")
    snapshot_views(root, "v1")
    _write_views(root, context="# ctx v2\n", durable="# dur v2\n")
    snapshot_views(root, "v2")

    assert "v1" in _show(root, "HEAD~1", view)
