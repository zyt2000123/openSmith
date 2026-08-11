"""Keep the suite out of the developer's real ``~/.agent-smith`` install.

A ``ToolGuard`` built without an explicit log path appends to the production,
hash-chained ``audit.jsonl`` (see ``engine/tests/conftest.py`` for the same
guard on the engine suite).  Individual tests here already called
``reset_paths`` by hand; doing it once for every test makes the isolation the
default rather than something each new test has to remember.

``project_root`` stays real: tests read shipped assets from ``agents/``.
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
