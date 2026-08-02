from __future__ import annotations

import shutil
from pathlib import Path

from common import config as common_config


def smith_profile_dir() -> Path:
    """Return the one writable runtime profile owned by Smith."""
    return common_config.PATHS.agent_dir


def init_smith_profile_files(
    *,
    profile_seed_dir: Path,
    name: str,
    role: str,
    description: str,
) -> None:
    dest = smith_profile_dir()
    dest.mkdir(parents=True, exist_ok=True)

    if profile_seed_dir.is_dir():
        for item in profile_seed_dir.rglob("*"):
            relative = item.relative_to(profile_seed_dir)
            target = dest / relative
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.is_file() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)

    for sub in ("memory", "sessions", "skills"):
        (dest / sub).mkdir(exist_ok=True)
