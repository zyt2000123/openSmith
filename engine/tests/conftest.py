"""Keep the suite out of the developer's real ``~/.agent-smith`` install.

``AuditLog()`` falls back to ``DATA_DIR`` when no path is given, so every
``ToolGuard`` built without an explicit log appends to the production,
hash-chained ``audit.jsonl``.  One run of ``tests/safety`` + ``tests/tool``
added ~99 synthetic entries to the real log; the accumulated noise buries
genuine tool calls when a past session is later debugged, and it inflates a
chain whose whole purpose is to be an accurate record of what ran.

``project_root`` deliberately stays real: tests read shipped assets from
``agents/`` (tools, identities, skills, safety rules).  Only the writable data
root moves.
"""
from __future__ import annotations

import pytest

from common.config import reset_paths
from common.paths import PRIVATE_DIR_MODE, AppPaths


@pytest.fixture(autouse=True)
def isolate_runtime_data_dir(tmp_path):
    """Point the runtime data root at a per-test temporary directory."""
    project_root = AppPaths.defaults().project_root
    data_dir = tmp_path / "agent-smith"
    data_dir.mkdir(mode=PRIVATE_DIR_MODE)
    reset_paths(AppPaths(data_dir=data_dir, project_root=project_root))
    try:
        yield data_dir
    finally:
        reset_paths(None)
