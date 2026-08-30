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
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Only these paths ever enter the index, and they are named explicitly on every
# call.  The agent root also holds config.yaml, toolbox.md and the rest of the
# profile; `git add -A` there would enrol whatever lands in that directory next.
# A whitelist fails closed -- a new sensitive file stays out even if nobody
# remembers to write an ignore rule for it.
#
# recent.jsonl joins the two rendered views because Dream's reclaim truncates
# it: restoring a conclusion without the evidence it was drawn from leaves the
# two out of step, and the next compile would reread a log that no longer holds
# what the restored document cites.
TRACKED_VIEWS: tuple[str, ...] = ("context.md", "memory/durable.md", "memory/recent.jsonl")

_TIMEOUT_SECONDS = 15.0

# A restore target is handed straight to git.  Anything that is not a plain
# object name could be read as an option (`--upload-pack=...`), and `--` guards
# the pathspec position, not the <tree-ish> one.
_REF_PATTERN = re.compile(r"\A[0-9a-fA-F]{7,40}\Z")

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


def snapshot_baseline(agent_root: Path, message: str) -> bool:
    """Commit the views as they stand, but only before the repository exists.

    A post-write snapshot can never reach the state that preceded the *first*
    write: the repository is created after that write, so the original document
    lands in no commit at all.  One baseline closes that gap.

    Every later write is deliberately not preceded by a snapshot.  The previous
    post-write commit already holds exactly those bytes, so a pre-write snapshot
    would be a guaranteed nothing-to-commit -- while still forking two git
    processes on a path that runs on every accepted draft.  The existence check
    here is a stat, not a subprocess.
    """
    if (agent_root / ".git").exists():
        return False
    return snapshot_views(agent_root, message)


# Nothing else here ever packs.  Git's own auto-gc rides on fetch/merge/rebase/
# receive-pack; this repository only ever commits, so a snapshot's objects stay
# loose for the life of the install -- and loose objects carry no delta, so every
# snapshot keeps a *full* zlib copy of recent.jsonl (1-5 MB inside the 7-day
# retention window) forever.
#
# `git gc --auto` is the obvious trigger and does not fire here: it estimates the
# loose count from one 1/256 shard (`objects/17/`), so it stays dormant until
# roughly 500 objects have piled up whatever `gc.auto` says -- measured at 48
# loose objects with `-c gc.auto=1`: nothing packed.  Counting the shards here
# instead costs 256 opendir calls on a path that runs every ~50 memory turns.
_GC_LOOSE_OBJECT_LIMIT = 50


def compact_snapshots(agent_root: Path) -> bool:
    """Repack the snapshot repository once loose objects have accumulated.

    Packing never costs a snapshot: `gc` prunes only *unreachable* objects, and
    every commit here is reachable from HEAD, so everything ``list_snapshots``
    can name stays restorable -- including the evidence prefix Dream reclaimed.
    """
    objects = agent_root / ".git" / "objects"
    if not objects.is_dir():
        return False
    try:
        if sum(1 for _ in objects.glob("??/*")) <= _GC_LOOSE_OBJECT_LIMIT:
            return False
        # No _RUN_CONFIG: gc writes no commit, so it needs no identity.
        gc = _git(agent_root, "gc", "--quiet")
        if gc.returncode != 0:
            logger.warning("memory snapshot: git gc failed: %s", gc.stderr.strip()[:200])
            return False
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        # Same discipline as every other call here: a repo that cannot be packed
        # is a growing repo, never a failed memory write.
        logger.warning("memory snapshot compaction unavailable: %s", exc)
        return False


@dataclass(frozen=True)
class MemorySnapshot:
    """One recoverable point in the memory history."""

    ref: str
    timestamp: str
    message: str


def list_snapshots(agent_root: Path, limit: int = 20) -> list[MemorySnapshot]:
    """Most recent snapshots first; empty when git or the repository is absent."""
    if limit <= 0:
        return []
    try:
        log = _git(
            agent_root,
            "log",
            f"-{limit}",
            # A unit separator, because a commit subject may contain anything a
            # view name does -- splitting on a printable delimiter would cut the
            # message in two.
            "--format=%H%x1f%cI%x1f%s",
            "--",
            *TRACKED_VIEWS,
        )
        if log.returncode != 0:
            logger.warning("memory snapshot: git log failed: %s", log.stderr.strip()[:200])
            return []
        snapshots = []
        for line in log.stdout.splitlines():
            parts = line.split("\x1f", 2)
            if len(parts) == 3:
                snapshots.append(
                    MemorySnapshot(ref=parts[0], timestamp=parts[1], message=parts[2])
                )
        return snapshots
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("memory snapshot unavailable: %s", exc)
        return []


def restore_snapshot(agent_root: Path, ref: str) -> bool:
    """Roll the tracked views back to *ref*; return whether anything was restored.

    The current state is snapshotted first, so undoing a bad Dream is itself
    undoable.  A restore that destroyed the only copy of what it replaced would
    move the data loss one step along instead of preventing it.
    """
    if not _REF_PATTERN.match(ref):
        logger.warning("memory snapshot: refusing to restore malformed ref %r", ref)
        return False
    try:
        if not (agent_root / ".git").exists():
            logger.warning("memory snapshot: no repository under %s", agent_root)
            return False

        snapshot_views(agent_root, f"memory: state before restoring {ref[:12]}")

        restored = []
        for name in TRACKED_VIEWS:
            # A file the commit never had is left alone rather than deleted:
            # `git checkout <ref> -- <path>` fails for it, and removing live
            # evidence to complete a rollback trades one loss for another.
            if _git(agent_root, "checkout", ref, "--", name).returncode == 0:
                restored.append(name)
        if not restored:
            logger.warning("memory snapshot: nothing restored from %s", ref)
            return False

        snapshot_views(agent_root, f"memory: restored {ref[:12]} ({len(restored)} file(s))")
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("memory snapshot restore unavailable: %s", exc)
        return False
