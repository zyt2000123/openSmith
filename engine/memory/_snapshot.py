"""Git snapshots of the rendered memory views.

The rendered views had exactly one generation of recovery before this: a single
``.bak`` next to each file, plus ``memory_history.jsonl`` -- which records only
``old_hash``/``new_hash``, never the text.  Two consecutive bad writes therefore
destroyed the last good document beyond recovery, leaving an audit trail of
digests that cannot be turned back into content.

A snapshot is an audit and undo aid, never part of the memory contract: memory
must keep working when git is absent, the repo is corrupt, or an index lock is
held.  Every failure path here logs and returns False.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Only these two paths ever enter the index, and they are named explicitly on
# every call.  The agent root also holds config.yaml, toolbox.md and the rest of
# the profile; `git add -A` there would enrol whatever lands in that directory
# next.  A whitelist fails closed -- a new sensitive file stays out even if
# nobody remembers to write an ignore rule for it.
TRACKED_VIEWS: tuple[str, ...] = ("context.md", "memory/durable.md")

_TIMEOUT_SECONDS = 15.0

# A fresh machine may have no committer identity, and a configured GPG key would
# block on a passphrase prompt.  Both are supplied per invocation so the
# snapshot never depends on -- or mutates -- the user's global git config.
_RUN_CONFIG: tuple[str, ...] = (
    "-c", "user.name=Agent-Smith",
    "-c", "user.email=smith@agent-smith.local",
    "-c", "commit.gpgsign=false",
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", "-C", str(root), *args),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )


def snapshot_views(agent_root: Path, message: str) -> bool:
    """Commit the rendered views under *agent_root*; return whether a commit landed.

    *agent_root* is the directory holding ``context.md`` and ``memory/``, i.e.
    ``memory_dir.parent``.  Initialises the repository on first use so existing
    installs need no migration.
    """
    try:
        if not (agent_root / ".git").exists():
            init = _git(agent_root, "init", "-q")
            if init.returncode != 0:
                logger.warning(
                    "memory snapshot: git init failed: %s", init.stderr.strip()[:200]
                )
                return False

        present = [name for name in TRACKED_VIEWS if (agent_root / name).is_file()]
        if not present:
            return False

        staged = _git(agent_root, "add", "--", *present)
        if staged.returncode != 0:
            logger.warning(
                "memory snapshot: git add failed: %s", staged.stderr.strip()[:200]
            )
            return False

        # The pathspec is repeated on commit so an index polluted by anything
        # else -- a stray `git add` run by hand in this directory -- cannot ride
        # along into the snapshot.
        commit = _git(
            agent_root, *_RUN_CONFIG, "commit", "-m", message, "--", *present
        )
        if commit.returncode == 0:
            return True

        combined = f"{commit.stdout}{commit.stderr}".lower()
        if "nothing to commit" in combined or "no changes added" in combined:
            # The views are byte-identical to the last snapshot: a no-op, not a
            # failure.  Compilation writes on every accepted draft, including
            # ones that reproduce the previous document.
            return False
        logger.warning(
            "memory snapshot: git commit failed: %s", commit.stderr.strip()[:200]
        )
        return False
    except (OSError, subprocess.SubprocessError) as exc:
        # FileNotFoundError covers "git is not installed", TimeoutExpired covers
        # a hung index lock.  Neither may interrupt a memory write.
        logger.warning("memory snapshot unavailable: %s", exc)
        return False
