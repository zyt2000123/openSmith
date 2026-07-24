from __future__ import annotations

import stat
from pathlib import Path

import pytest

from engine.execution.pipeline.checkpoint import SessionCheckpoint, SessionStateManager


def test_session_checkpoints_are_private(tmp_path: Path):
    manager = SessionStateManager(tmp_path)
    manager.save(SessionCheckpoint(
        agent_id="agent",
        session_id="session",
        identity_id="smith",
        route_id="direct",
        skill_chain_index=-1,
        context={"user_message": "sensitive"},
        timestamp="2026-07-15T00:00:00+00:00",
    ))

    state_dir = tmp_path / "sessions" / ".state"
    checkpoint = state_dir / "session.json"
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600


def test_session_state_rejects_symlinked_directory(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / ".state").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink"):
        SessionStateManager(tmp_path)
