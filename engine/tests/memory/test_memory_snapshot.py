"""Snapshots must recover a document that two later writes overwrote."""

from __future__ import annotations

import json
import subprocess

import pytest

from engine.memory._snapshot import (
    _GC_LOOSE_OBJECT_LIMIT,
    TRACKED_VIEWS,
    compact_snapshots,
    list_snapshots,
    restore_snapshot,
    snapshot_baseline,
    snapshot_views,
)


def _agent_root(tmp_path):
    root = tmp_path / "agent"
    (root / "memory").mkdir(parents=True)
    return root


def _write_views(root, *, context: str, durable: str, recent: str | None = None) -> None:
    (root / "context.md").write_text(context, encoding="utf-8")
    (root / "memory" / "durable.md").write_text(durable, encoding="utf-8")
    # The evidence log is tracked alongside the rendered views, so a helper that
    # left it out would make every "all tracked views" assertion vacuous for it.
    # It derives from *durable* so a changed document implies changed evidence,
    # which is what the real pipeline produces.
    line = recent if recent is not None else json.dumps({"note": durable.strip()}) + "\n"
    (root / "memory" / "recent.jsonl").write_text(line, encoding="utf-8")


def _loose_objects(root) -> int:
    return sum(1 for _ in (root / ".git" / "objects").glob("??/*"))


def _packs(root) -> list:
    return list((root / ".git" / "objects" / "pack").glob("*.pack"))


def _snapshot_past_the_gc_limit(root) -> None:
    """Take snapshots until the repository holds more loose objects than the limit."""
    for i in range(_GC_LOOSE_OBJECT_LIMIT + 1):
        _write_views(root, context=f"# ctx v{i}\n", durable=f"# dur v{i}\n")
        snapshot_views(root, f"memory: durable (v{i})")
        if _loose_objects(root) > _GC_LOOSE_OBJECT_LIMIT:
            return
    raise AssertionError("snapshots stopped accumulating loose objects")


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


def test_only_whitelisted_views_ever_enter_the_repository(tmp_path):
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


def test_baseline_captures_the_state_before_the_first_write_only(tmp_path):
    """The pre-compilation document is in no post-write commit: the repository
    does not exist until after that write."""
    root = _agent_root(tmp_path)
    _write_views(root, context="# ctx original\n", durable="# dur original\n")

    assert snapshot_baseline(root, "memory: baseline") is True

    _write_views(root, context="# ctx compiled\n", durable="# dur compiled\n")
    snapshot_views(root, "memory: durable (written)")

    assert "original" in _show(root, "HEAD~1", "memory/durable.md")
    # Later writes must not pay for a baseline that could only ever commit the
    # bytes the previous post-write snapshot already holds.
    assert snapshot_baseline(root, "memory: baseline again") is False
    assert len(list_snapshots(root)) == 2


def test_baseline_never_shells_out_once_the_repository_exists(tmp_path, monkeypatch):
    """Guards the cost, not just the outcome: this runs on every accepted draft."""
    root = _agent_root(tmp_path)
    _write_views(root, context="# ctx\n", durable="# dur\n")
    snapshot_views(root, "first")

    def _forbidden(*args, **kwargs):
        raise AssertionError("baseline must not spawn git once the repo exists")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    assert snapshot_baseline(root, "memory: baseline") is False


def test_list_snapshots_returns_history_newest_first(tmp_path):
    root = _agent_root(tmp_path)
    _write_views(root, context="# ctx v1\n", durable="# dur v1\n")
    snapshot_views(root, "memory: durable (v1)")
    _write_views(root, context="# ctx v2\n", durable="# dur v2\n")
    snapshot_views(root, "memory: durable (v2)")

    snapshots = list_snapshots(root)

    assert [s.message for s in snapshots] == ["memory: durable (v2)", "memory: durable (v1)"]
    assert all(s.timestamp for s in snapshots)
    assert len({s.ref for s in snapshots}) == 2


def test_list_snapshots_is_empty_without_a_repository(tmp_path):
    """Listing is a read of a recovery aid, not part of the memory contract."""
    assert list_snapshots(_agent_root(tmp_path)) == []


def test_restore_rolls_back_and_stays_undoable(tmp_path):
    """Undoing a bad write must not destroy the only copy of what it replaced."""
    root = _agent_root(tmp_path)
    _write_views(root, context="# ctx good\n", durable="# dur good\n")
    snapshot_views(root, "good")
    _write_views(root, context="# ctx ruined\n", durable="# dur ruined\n")
    snapshot_views(root, "ruined")

    good = list_snapshots(root)[-1]
    assert restore_snapshot(root, good.ref) is True

    assert (root / "memory" / "durable.md").read_text(encoding="utf-8") == "# dur good\n"
    assert (root / "context.md").read_text(encoding="utf-8") == "# ctx good\n"
    # The ruined document is still reachable: the restore snapshotted it first,
    # so a restore aimed at the wrong commit is itself recoverable.
    assert any("ruined" in _show(root, s.ref, "memory/durable.md") for s in list_snapshots(root))


def test_restore_rejects_a_ref_that_could_be_read_as_an_option(tmp_path):
    """`--` guards the pathspec position, never the <tree-ish> one."""
    root = _agent_root(tmp_path)
    _write_views(root, context="# ctx\n", durable="# dur\n")
    snapshot_views(root, "only")

    for ref in ("--upload-pack=touch /tmp/pwned", "HEAD", "main", "", "../etc", "zzzzzzz"):
        assert restore_snapshot(root, ref) is False

    assert (root / "memory" / "durable.md").read_text(encoding="utf-8") == "# dur\n"


def test_restore_without_a_repository_is_false_not_an_exception(tmp_path):
    assert restore_snapshot(_agent_root(tmp_path), "0" * 40) is False


def test_reclaimed_evidence_is_recoverable(tmp_path):
    """Dream's reclaim is atomic but not undoable on its own: the cleanup journal
    replays a half-finished truncation, it does not bring the prefix back."""
    from engine.memory.dream import DreamReport, _cleanup_log

    root = _agent_root(tmp_path)
    memory_dir = root / "memory"
    old = "2024-01-01T00:00:00+00:00"
    events = [
        json.dumps({"timestamp": old, "type": "work", "task": f"event {i}"}) for i in range(2)
    ]
    _write_views(
        root,
        context="# ctx\n",
        durable="# dur\n",
        recent="\n".join(events) + "\n",
    )
    (memory_dir / ".compile_offset").write_text("2", encoding="utf-8")
    (memory_dir / ".compile_offset_context").write_text("2", encoding="utf-8")

    report = DreamReport()
    assert _cleanup_log(memory_dir, report) == 2
    assert (memory_dir / "recent.jsonl").read_text(encoding="utf-8") == ""

    # The evidence is gone from disk but reachable in history, together with the
    # views that cite it — restoring one without the other is what makes a
    # rollback leave memory inconsistent.
    restorable = [
        s for s in list_snapshots(root) if "event 0" in _show(root, s.ref, "memory/recent.jsonl")
    ]
    assert restorable, "the reclaimed prefix left no recoverable snapshot"

    assert restore_snapshot(root, restorable[0].ref) is True
    assert "event 0" in (memory_dir / "recent.jsonl").read_text(encoding="utf-8")


def test_snapshot_history_is_repacked_instead_of_growing_forever(tmp_path):
    """Every writer here only commits, and `git commit` triggers no auto-gc, so
    each snapshot's *full* copy of recent.jsonl stayed a delta-free loose blob
    for the life of the install -- growth with no ceiling and no reclaim."""
    root = _agent_root(tmp_path)
    _snapshot_past_the_gc_limit(root)

    assert not _packs(root), "committing must not be assumed to pack anything"
    before = _loose_objects(root)

    assert compact_snapshots(root) is True

    assert _loose_objects(root) < before
    assert _loose_objects(root) <= _GC_LOOSE_OBJECT_LIMIT
    assert _packs(root)

    # Packing moves objects, it never drops a snapshot: `gc` prunes only
    # unreachable objects and every commit here hangs off HEAD.  The undo path
    # must survive compaction or it buys space by destroying the recovery it
    # exists for.
    history = list_snapshots(root, limit=_GC_LOOSE_OBJECT_LIMIT)
    assert len(history) > 1
    oldest = history[-1]
    assert "v0" in _show(root, oldest.ref, "memory/durable.md")
    assert restore_snapshot(root, oldest.ref) is True
    assert (root / "memory" / "durable.md").read_text(encoding="utf-8") == "# dur v0\n"


def test_dream_is_what_compacts_the_snapshot_repository(tmp_path):
    """Compaction has to sit on a path that actually runs. Dream is the only
    low-frequency pass; every other snapshot writer is on a per-turn path that
    cannot afford a repack."""
    import asyncio

    from engine.memory.dream import run_dream

    root = _agent_root(tmp_path)
    _snapshot_past_the_gc_limit(root)

    # run_dream never touches the LLM: it sanitizes and reclaims, both local.
    asyncio.run(run_dream(root / "memory", None))  # type: ignore[arg-type]

    assert _packs(root)
    assert _loose_objects(root) <= _GC_LOOSE_OBJECT_LIMIT


@pytest.mark.parametrize("view", TRACKED_VIEWS)
def test_each_view_is_recoverable_independently(tmp_path, view):
    root = _agent_root(tmp_path)
    _write_views(root, context="# ctx v1\n", durable="# dur v1\n")
    snapshot_views(root, "v1")
    _write_views(root, context="# ctx v2\n", durable="# dur v2\n")
    snapshot_views(root, "v2")

    assert "v1" in _show(root, "HEAD~1", view)
