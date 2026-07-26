from __future__ import annotations

from pathlib import Path

import pytest

import engine.tool.snapshot as snapshot_mod
from engine.tool.snapshot import _MAX_VERSIONS_PER_FILE, FileSnapshot, get_snapshot


@pytest.fixture()
def snap(tmp_path: Path, monkeypatch) -> FileSnapshot:
    """FileSnapshot writing backups under tmp_path instead of ~/.agent-smith."""
    import common.config

    monkeypatch.setattr(common.config, "DATA_DIR", tmp_path / "data")
    return FileSnapshot("test-session")


def test_track_and_rewind_restores_previous_content(snap: FileSnapshot, tmp_path: Path):
    target = tmp_path / "file.txt"
    target.write_text("v1", encoding="utf-8")

    assert snap.track(str(target))
    target.write_text("v2", encoding="utf-8")

    assert snap.rewind(str(target))
    assert target.read_text(encoding="utf-8") == "v1"


def test_track_caps_versions_and_prunes_backup_files(snap: FileSnapshot, tmp_path: Path):
    target = tmp_path / "file.txt"
    extra = 5

    for i in range(_MAX_VERSIONS_PER_FILE + extra):
        target.write_text(f"content-{i}", encoding="utf-8")
        assert snap.track(str(target))

    import os

    resolved = os.path.realpath(str(target))
    versions = snap._tracked[resolved]
    assert len(versions) == _MAX_VERSIONS_PER_FILE
    # Pruned backups are removed from disk; surviving ones still exist.
    assert all((snap._backup_dir / name).is_file() for name in versions)
    assert len(list(snap._backup_dir.iterdir())) == _MAX_VERSIONS_PER_FILE

    # After pruning, new backups must not collide with surviving names
    # (regression: version numbers derived from len(versions) would reuse
    # and overwrite a still-referenced backup).
    target.write_text("newest", encoding="utf-8")
    assert snap.track(str(target))
    assert len(set(snap._tracked[resolved])) == _MAX_VERSIONS_PER_FILE

    # rewind restores the content saved by the latest track call
    target.write_text("clobbered", encoding="utf-8")
    assert snap.rewind(str(target))
    assert target.read_text(encoding="utf-8") == "newest"


def test_track_missing_file_marks_creation_and_rewind_deletes(
    snap: FileSnapshot, tmp_path: Path
):
    target = tmp_path / "new.txt"
    assert snap.track(str(target))  # file does not exist yet
    target.write_text("created", encoding="utf-8")

    assert snap.rewind(str(target))
    assert not target.exists()


def test_get_snapshot_reuses_session_instance(tmp_path: Path, monkeypatch):
    import common.config

    monkeypatch.setattr(common.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(snapshot_mod, "_active_snapshots", {})

    first = snapshot_mod.get_snapshot("s1")
    assert snapshot_mod.get_snapshot("s1") is first
    assert snapshot_mod.get_snapshot("s2") is not first


def test_snapshot_rejects_traversal_session_ids():
    with pytest.raises(ValueError):
        FileSnapshot("../escape")
    with pytest.raises(ValueError):
        get_snapshot("..")
