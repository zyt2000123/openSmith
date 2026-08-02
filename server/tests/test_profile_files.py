from __future__ import annotations

from pathlib import Path

from common.config import SMITH_PROFILE_DIR

from app.infrastructure import profile_files


def test_profile_seed_only_fills_missing_files_and_preserves_user_content(
    monkeypatch,
    tmp_path: Path,
) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "role.md").write_text("seed role", encoding="utf-8")
    (seed / "style.md").write_text("seed style", encoding="utf-8")

    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "role.md").write_text("user role", encoding="utf-8")
    monkeypatch.setattr(profile_files, "smith_profile_dir", lambda: profile)

    profile_files.init_smith_profile_files(
        profile_seed_dir=seed,
    )

    assert (profile / "role.md").read_text(encoding="utf-8") == "user role"
    assert (profile / "style.md").read_text(encoding="utf-8") == "seed style"


def test_shipped_smith_workflow_is_seeded_as_default(
    monkeypatch,
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    monkeypatch.setattr(profile_files, "smith_profile_dir", lambda: profile)

    profile_files.init_smith_profile_files(
        profile_seed_dir=SMITH_PROFILE_DIR,
    )

    workflow = (profile / "workflow.md").read_text(encoding="utf-8")
    assert "## 编码执行纪律" in workflow
    assert "最小实现优先" in workflow
    assert "每一处改动都应能追溯到用户请求" in workflow
