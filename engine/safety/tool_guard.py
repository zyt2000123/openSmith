"""Tool safety guard — permission levels, path checking, audit logging."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from common.hash_chain import ChainVerification, HashChainLog, verify_chain
from common.sensitive_rules import (
    CREDENTIAL_CONFIG_FILES,
    CREDENTIAL_DIRS,
    CREDENTIAL_PATH_PAIRS,
    ENV_TEMPLATE_SUFFIXES,
    GIT_CREDENTIAL_FILES,
    PRIVATE_KEY_EXTENSIONS,
    SENSITIVE_WRITE_FILES,
    SSH_KEY_NAME_RE,
    is_sensitive_path,
    is_sensitive_read_name,
    is_sensitive_write_name,
    match_credential_pair,
    runtime_credential_paths,
)
from common.vault import VaultStore, extract_vault_placeholders
from engine.safety.approval import (
    ApprovalScope,
    _SECRET_FLAG_RE,
    _is_sensitive_argument_name,
    _is_secret_value,
    _redact_secret_text,
    current_approval_context,
)
from engine.safety.risk import RiskTier, risk_for_approval
from engine.safety.rules import (
    BucketKey,
    RepeatSensitiveChecker,
    SafetyConfig,
    classify_sensitive_bucket,
)
from engine.safety.taint import (
    EgressTarget,
    SessionTaintStore,
    detect_secret_in_text,
    resolve_egress_target,
)
from engine.tool.interface import ToolCall, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)


# ── Permission Levels (req #2: default-deny + tiered approval) ──

class PermissionLevel(Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    DESTRUCTIVE = "destructive"


@dataclass
class GuardResult:
    allowed: bool
    reason: str = ""
    level: PermissionLevel = PermissionLevel.READ
    needs_confirmation: bool = False
    # True when the call passed hard safety checks but must wait for a user
    # approval before the provider is invoked.
    approval_required: bool = False
    # True when the only issue is the active working-directory boundary. A
    # session-level trusted whitelist may extend that boundary; high-risk
    # approvals remain visible even when a path is otherwise reachable.
    boundary_block: bool = False
    # The user-visible, one-shot capability requested for this call. It is
    # carried through the approval broker and re-checked by ToolRegistry.
    approval_scope: ApprovalScope | None = None
    # Risk tier used to triage the approval flow (see engine.safety.risk).
    # HIGH/CRITICAL approvals are never eligible for session-whitelist caching.
    risk: RiskTier = RiskTier.ROUTINE
    # Observability & decision metadata for declarative rules, taint, and repeat guard.
    rule_id: str | None = None
    taint_sources: tuple[str, ...] = ()
    repeat_count: int = 0
    repeat_bucket: BucketKey | None = None


# ── Path guard (req #3: symlink, traversal, sensitive files) ──

def _ordered_unique(*values: str) -> tuple[str, ...]:
    """Deduplicate while preserving order, for stable user-facing reasons.

    A ``set`` here would make the refusal message name whichever spelling the
    hash order happened to yield.
    """
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return tuple(seen)


def _casefolded(path: Path) -> Path:
    """Lowercase a path, for security comparisons only — never for I/O.

    macOS (APFS) and Windows are case-insensitive but case-preserving, so
    ``.GIT/hooks/pre-commit`` reaches the very same inode as ``.git/...`` while
    ``Path.resolve()`` faithfully preserves the caller's casing.  Any guard that
    compares path components therefore has to fold case first, or the block is
    bypassable by simply retyping the path.  On case-sensitive filesystems this
    can only ever over-block, never under-block.
    """
    return Path(str(path).lower())


_HARDLINK_SCAN_SKIP = frozenset({"objects", "node_modules", ".venv", "__pycache__"})
# Files git always keeps in a real git directory (plain repo, worktree gitdir, or
# submodule gitdir all carry HEAD).
_GIT_DIR_MARKERS = ("HEAD", "config", "commondir")


def _looks_like_git_dir(path: Path) -> bool:
    """True when ``path`` is plausibly a git directory rather than any directory."""
    try:
        if not path.is_dir():
            return False
        return any((path / marker).exists() for marker in _GIT_DIR_MARKERS)
    except OSError:
        return False


def _git_dirs_for(working_dir: Path) -> list[Path]:
    """Resolve ``.git`` whether it is a directory or a worktree/submodule pointer.

    In a ``git worktree`` (and in submodules) ``.git`` is a *file* containing
    ``gitdir: <path>``, and the hooks that matter live in the main repository —
    so treating a non-directory ``.git`` as "nothing to scan" left the hard-link
    check inert for exactly the layout this repository itself uses.
    """
    git_path = working_dir / ".git"
    if git_path.is_dir():
        return [git_path]
    if not git_path.is_file():
        return []
    try:
        text = git_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return []
    if not text.startswith("gitdir:"):
        return []
    pointer = Path(text[len("gitdir:"):].strip()).expanduser()
    if not pointer.is_absolute():
        pointer = working_dir / pointer
    try:
        pointer = pointer.resolve()
    except OSError:
        return []
    candidates = [pointer]
    # A worktree's gitdir is <main>/.git/worktrees/<id>; hooks live in the common
    # dir two levels up.  (A submodule's is <main>/.git/modules/<name>, which
    # carries its own hooks/ and must not be lifted.)
    if pointer.parent.name == "worktrees":
        candidates.append(pointer.parent.parent)
    # Only follow a pointer that actually looks like a git directory.  This file
    # is not covered by any write guard — it arrives with a cloned repo — so an
    # unvalidated pointer would let untrusted content aim the walk below at any
    # path, turning every nlink>1 write check into a scan of, say, /usr.
    return [path for path in candidates if _looks_like_git_dir(path)]


def _shares_inode_with_protected_file(
    target: Path,
    working_dir: Path | None,
    extra_protected_roots: tuple[Path, ...] = (),
) -> bool:
    """True when ``target`` is a hard link into a protected location.

    A hard link gives one inode a second name, so a harmless-looking path can
    write straight through to ``.git/hooks/pre-commit``; name-based checks cannot
    see that.  Rejecting *every* multiply-linked file would be simpler but breaks
    trees that use hard links normally — pnpm's content-addressed store, ccache,
    ``cp -al`` backups — so confirm the inode really is reachable from a
    protected directory before refusing.

    Short-circuits on ``st_nlink == 1``, which covers virtually every write, so
    the walk below never runs on the hot path.  ``.git/objects`` is skipped:
    those blobs are content-addressed, immutable and checksum-verified by git.
    """
    try:
        info = os.lstat(target)
    except OSError:
        return False
    if info.st_nlink < 2 or not os.path.isfile(target):
        return False

    identity = (info.st_dev, info.st_ino)
    home = Path.home()
    roots = [home / name for name in FileGuard._ALWAYS_BLOCKED]
    roots.append(_PLATFORM_DATA_ROOT)
    if working_dir is not None:
        roots.extend(_git_dirs_for(working_dir))
        roots.extend(working_dir / name for name in FileGuard._ALWAYS_BLOCKED)
    roots.extend(extra_protected_roots)

    # Secret-bearing files (`.env`, `.npmrc`, …) live as plain files, not
    # under a blocked directory, so the walk above never sees them.  A
    # ``ln .env x`` then ``edit_file(x)`` would name-check clean and inode-scan
    # clean.  Compare directly against the well-known sensitive files instead.
    sensitive_candidates = [home / name for name in FileGuard._SENSITIVE_WRITE]
    if working_dir is not None:
        sensitive_candidates.extend(
            working_dir / name for name in FileGuard._SENSITIVE_WRITE
        )
    for candidate in sensitive_candidates:
        try:
            candidate_info = os.lstat(candidate)
        except OSError:
            continue
        if (candidate_info.st_dev, candidate_info.st_ino) == identity:
            return True

    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in _HARDLINK_SCAN_SKIP]
            for filename in filenames:
                try:
                    candidate = os.lstat(os.path.join(dirpath, filename))
                except OSError:
                    continue
                if (candidate.st_dev, candidate.st_ino) == identity:
                    return True
    return False


_SENSITIVE_READ_EXTENSIONS = PRIVATE_KEY_EXTENSIONS
_ENV_TEMPLATE_SUFFIXES = ENV_TEMPLATE_SUFFIXES
_SENSITIVE_KEY_NAME_RE = SSH_KEY_NAME_RE
_GIT_CREDENTIAL_FILES = GIT_CREDENTIAL_FILES
_CREDENTIAL_CONFIG_NAMES = CREDENTIAL_CONFIG_FILES
_CREDENTIAL_PATH_PAIRS = CREDENTIAL_PATH_PAIRS


def _credential_pair(path: Path) -> str | None:
    """Return the ``parent/child`` credential directory this path enters, if any."""
    return match_credential_pair(path)


def _is_sensitive_read_name(name: str) -> bool:
    """Whether a case-folded basename warrants high-risk approval when read."""
    return is_sensitive_read_name(name)


class FileGuard:
    _ALWAYS_BLOCKED = CREDENTIAL_DIRS
    _SENSITIVE_WRITE = SENSITIVE_WRITE_FILES
    _SENSITIVE_DIRS = frozenset({".git"})

    def __init__(self, allowed_dirs: list[Path] | None = None):
        self._working_dir: Path | None = None
        self._extra_roots: tuple[Path, ...] = ()
        self._non_delegable_write_roots: tuple[Path, ...] = ()
        self.set_allowed_dirs(allowed_dirs)

    def set_allowed_dirs(self, allowed_dirs: list[Path] | None) -> None:
        """Replace the ordinary directory boundary.

        ``None`` means "not configured" and keeps the permissive default.  An
        empty list is a deliberate deny-all baseline for a caller that intends to
        reach exactly one path through :meth:`SessionWhitelist.allow_file` —
        conflating it with ``None`` handed that caller ``[home, /tmp, cwd]``
        instead, silently turning its whitelist into a no-op.
        """
        if allowed_dirs is None:
            self._allowed = [Path.home().resolve(), Path("/tmp").resolve(), Path.cwd().resolve()]
        else:
            self._allowed = [p.resolve() for p in allowed_dirs]

    def set_working_directory(
        self,
        working_dir: Path,
        *,
        extra_roots: Iterable[Path] = (),
    ) -> None:
        root = Path(working_dir).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"working directory does not exist: {working_dir}")
        self._working_dir = root
        resolved_extra: list[Path] = []
        for extra in extra_roots:
            resolved = Path(extra).expanduser().resolve()
            if resolved != root and resolved.is_dir() and resolved not in resolved_extra:
                resolved_extra.append(resolved)
        self._extra_roots = tuple(resolved_extra)
        self.set_allowed_dirs([root, *self._extra_roots])

    def set_non_delegable_write_roots(self, roots: list[Path]) -> None:
        """Configure paths that model tools may read but never modify.

        These roots protect the runtime's executable provider code.  They are
        checked before ordinary approval handling, so a one-shot user approval
        cannot turn into server-process code execution on a later request.
        """
        self._non_delegable_write_roots = tuple(
            Path(root).expanduser().resolve() for root in roots
        )

    @property
    def is_working_directory_scoped(self) -> bool:
        return self._working_dir is not None

    def is_inside_allowed_dirs(self, target: Path) -> bool:
        """Whether a resolved path lies within the ordinary allowed directories.

        The registry uses this to decide how much a one-shot path approval may
        whitelist: inside the workspace every sibling is already accessible, so
        directory-granularity is harmless; an external path must not silently
        grant the whole directory.
        """
        try:
            return any(target.is_relative_to(d) for d in self._allowed)
        except ValueError:
            return False

    def _is_non_delegable_write_target(self, target: Path, lexical: Path) -> bool:
        for root in self._non_delegable_write_roots:
            folded_root = _casefolded(root)
            for candidate in (target, lexical):
                try:
                    if _casefolded(candidate).is_relative_to(folded_root):
                        return True
                except ValueError:
                    continue
        return False

    @staticmethod
    def _path_approval(
        target: Path,
        *,
        writing: bool,
        reason: str,
        high_risk: bool = False,
        boundary_block: bool = False,
    ) -> GuardResult:
        return GuardResult(
            allowed=False,
            reason=reason,
            level=PermissionLevel.WRITE if writing else PermissionLevel.READ,
            needs_confirmation=True,
            approval_required=True,
            boundary_block=boundary_block,
            approval_scope=ApprovalScope.path(
                str(target),
                writing=writing,
                high_risk=high_risk,
            ),
            risk=risk_for_approval(
                level=PermissionLevel.WRITE.value if writing else PermissionLevel.READ.value,
                high_risk=high_risk,
            ),
        )

    def check_path(
        self,
        path_str: str,
        writing: bool = False,
        *,
        declared_path: str | None = None,
    ) -> GuardResult:
        """Evaluate one path against the guard's rules.

        ``declared_path`` is the same target as the caller spelled it, absolutized
        but not resolved.  It matters because ``ToolRegistry.normalize_call``
        hands this method an already-resolved ``path_str`` (so the guard and the
        provider agree on which file is touched), and resolving erases the very
        component names the rules below match on: with a symlinked ``.git``,
        ``lexical`` and ``target`` would otherwise both read
        ``<workspace>/gitstore/...`` and no ``.git`` rule could fire.  Passing the
        declared spelling restores the second view.  It is only ever used to
        *add* blocks, never to authorize: containment in ``_allowed`` is still
        decided on the resolved ``target`` alone.
        """
        try:
            # Shell expands a leading tilde before execution; mirror that here
            # so the guard evaluates the same target the command will touch.
            candidate = Path(path_str).expanduser()
            if not candidate.is_absolute() and self._working_dir is not None:
                candidate = self._working_dir / candidate
            if declared_path:
                declared = Path(declared_path).expanduser()
                if not declared.is_absolute() and self._working_dir is not None:
                    declared = self._working_dir / declared
                lexical = Path(os.path.abspath(str(declared)))
            else:
                lexical = Path(os.path.abspath(str(candidate)))
            target = candidate.resolve()
        except (ValueError, OSError):
            return GuardResult(allowed=False, reason=f"Invalid path: {path_str}")

        if _is_runtime_credential_path(target, lexical):
            return GuardResult(
                allowed=False,
                reason=(
                    "[runtime-secret-001] Agent runtime credential files are not "
                    "delegable to model tools"
                ),
                risk=RiskTier.CRITICAL,
            )
        if _shares_inode_with_runtime_credential(target):
            return GuardResult(
                allowed=False,
                reason=(
                    "[runtime-secret-001] Agent runtime credential files are not "
                    "delegable through a hard-link alias"
                ),
                risk=RiskTier.CRITICAL,
            )

        if writing and self._is_non_delegable_write_target(target, lexical):
            return GuardResult(
                allowed=False,
                reason=(
                    "[runtime-provider-001] Agent runtime tool provider files are "
                    "not delegable to model tools"
                ),
                risk=RiskTier.CRITICAL,
            )

        # Check the literal path as well as the resolved one: a credential
        # directory that is itself a symlink resolves to a name carrying no
        # ``.ssh``/``.aws`` component and would otherwise slip through.  Every
        # name/component rule below pairs the two views for the same reason; see
        # ``declared_path`` in this method's docstring for why ``lexical`` has to
        # be supplied by the caller rather than re-derived here.
        for part in (*target.parts, *lexical.parts):
            if part.lower() in self._ALWAYS_BLOCKED:
                return self._path_approval(
                    target,
                    writing=writing,
                    high_risk=True,
                    reason=(
                        f"Access to {part}/ contains user credentials and requires "
                        "high-risk approval"
                    ),
                )

        # Some credential stores are only identifiable as a directory *pair*
        # (``.config/gh``, ``Library/Keychains``).  Without this they fell
        # through to the ordinary boundary approval, which is ELEVATED — and an
        # ELEVATED path approval is cached by ToolRegistry, so one approval
        # handed the model repeat access to a GitHub or gcloud token store.
        for candidate in (target, lexical):
            pair = _credential_pair(candidate)
            if pair:
                return self._path_approval(
                    target,
                    writing=writing,
                    high_risk=True,
                    reason=(
                        f"Access to {pair}/ contains user credentials and requires "
                        "high-risk approval"
                    ),
                )

        if not writing:
            # Reads have no write-branch equivalent: read_file is
            # ``approval_policy="never"`` and the regex rules are
            # case-sensitive, so .ENV / .env.staging / key.PEM / id_rsa would
            # otherwise bypass approval entirely on case-insensitive filesystems.
            name = target.name.lower()
            # Resolved name first, then the declared one, so the reason text is
            # deterministic and names the file actually being opened when both
            # spellings are sensitive.
            for candidate_name in _ordered_unique(name, lexical.name.lower()):
                if _is_sensitive_read_name(candidate_name):
                    return self._path_approval(
                        target,
                        writing=False,
                        high_risk=True,
                        reason=(
                            f"Read of sensitive file {candidate_name} requires "
                            "high-risk approval"
                        ),
                    )
            # .git/config embeds remote URLs (which often carry credentials) and
            # .git/credentials stores them verbatim; both are free reads today.
            if (
                name in _GIT_CREDENTIAL_FILES or lexical.name.lower() in _GIT_CREDENTIAL_FILES
            ) and any(
                part.lower() == ".git" for part in (*target.parts, *lexical.parts)
            ):
                return self._path_approval(
                    target,
                    writing=False,
                    high_risk=True,
                    reason="Read of .git credential-bearing file requires high-risk approval",
                )

        if writing:
            # Pair the resolved and declared names/components, exactly as the
            # credential-directory branch above does.  Resolved-only was the gap:
            # a symlinked ``.git`` (or a symlinked ``.env``) resolves to an
            # innocuous name, and the write it authorizes is a git hook — code
            # that runs on the next git operation.
            for name in _ordered_unique(target.name.lower(), lexical.name.lower()):
                if is_sensitive_write_name(name):
                    return self._path_approval(
                        target,
                        writing=True,
                        high_risk=True,
                        reason=f"Write to sensitive file {name} requires high-risk approval",
                    )
            for part in (*target.parts, *lexical.parts):
                if part.lower() in self._SENSITIVE_DIRS:
                    return self._path_approval(
                        target,
                        writing=True,
                        high_risk=True,
                        reason=f"Write inside {part}/ requires high-risk approval",
                    )

            # Runtime-managed state is high-risk but, unlike the provider
            # credential files above, belongs to the local user and may be
            # touched after a visible, exact approval. The controlled memory
            # files retain their ordinary tool policy so lifecycle maintenance
            # does not create a second special path.
            try:
                # Case-fold the *block* test so a retyped ``.Agent-Smith`` cannot
                # slip past it; the memory allow-list below stays case-exact so
                # folding never widens the write exemption.
                folded_root = _casefolded(_PLATFORM_DATA_ROOT)
                lexical_platform = _casefolded(lexical).is_relative_to(folded_root)
                resolved_platform = _casefolded(target).is_relative_to(folded_root)
                lexical_memory = (
                    lexical.is_relative_to(_MEMORY_WRITE_ROOT)
                    and lexical.name in _MEMORY_WRITE_FILES
                )
                resolved_memory = (
                    target.is_relative_to(_MEMORY_WRITE_ROOT)
                    and target.name in _MEMORY_WRITE_FILES
                )
            except ValueError:
                lexical_platform = resolved_platform = True
                lexical_memory = resolved_memory = False

            if (lexical_platform or resolved_platform) and not (
                lexical_memory and resolved_memory
            ):
                return self._path_approval(
                    target,
                    writing=True,
                    high_risk=True,
                    reason=(
                        "[platform-state-001] Writing Agent-Smith runtime state "
                        "requires high-risk approval"
                    ),
                )

            # A hard link is a second name for one inode, so a path mentioning
            # nothing sensitive can still write straight into .git/hooks/ or
            # .ssh/.  Nothing above catches it: every check so far reasons about
            # path *names*, never inode identity.
            if _shares_inode_with_protected_file(
                target,
                self._working_dir,
                self._non_delegable_write_roots,
            ):
                return GuardResult(
                    allowed=False,
                    reason=(
                        f"[unsafe-alias-001] Cannot safely represent the write to "
                        f"{target.name}: it is a hard link to a protected file."
                    ),
                    risk=RiskTier.CRITICAL,
                )

        if any(target.is_relative_to(d) for d in self._allowed):
            return GuardResult(allowed=True)

        if self.is_working_directory_scoped:
            # A project-scoped run may inspect or change a user-selected
            # external path only after the live approval flow has bound that
            # exact normalized call.
            return self._path_approval(
                target,
                writing=writing,
                reason=(
                    f"Path {path_str} is outside the active working directory "
                    "and requires explicit user approval"
                ),
                boundary_block=True,
            )

        return self._path_approval(
            target,
            writing=writing,
            reason=f"Path {path_str} is outside the active directories and requires approval",
            boundary_block=True,
        )


# ── Audit log (req #6: every tool call logged) ──────────────

# One chain object per audit file for the whole process.  ``AuditLog`` is built
# per ``ToolGuard`` — i.e. per engine request (``build_engine_runtime``) — while
# every instance appends to the same install-wide ``audit.jsonl``.  Each
# ``HashChainLog`` caches seq/prev_hash in memory behind a *per-instance* lock,
# so two concurrent runs each assigned the same seq from the same prev_hash and
# forked the chain: ``verify()`` then reported a sequence gap on a log nobody
# tampered with.  Sharing the object makes that lock serialize every writer in
# this process; ``HashChainLog`` separately detects another process's appends.
_AUDIT_CHAINS: dict[Path, HashChainLog] = {}
_AUDIT_CHAINS_GUARD = threading.Lock()


def _shared_audit_chain(path: Path, namespace: str) -> HashChainLog:
    try:
        key = path.resolve()
    except OSError:
        key = Path(os.path.abspath(str(path)))
    with _AUDIT_CHAINS_GUARD:
        chain = _AUDIT_CHAINS.get(key)
        if chain is None:
            chain = HashChainLog(path, namespace=namespace, keep_handle=True)
            _AUDIT_CHAINS[key] = chain
        return chain


def close_audit_chains() -> None:
    """Seal and close every shared audit chain (server shutdown).

    Sealing here rather than per run is deliberate: the log is install-wide and
    concurrent runs interleave in it, so there is no per-run "head" to anchor —
    an anchor written mid-run would be stale the moment another run appended and
    would itself become a false ``anchor mismatch``.  A clean shutdown *is* a
    real boundary, and :meth:`HashChainLog.unseal` clears the anchor when
    appending resumes.
    """
    with _AUDIT_CHAINS_GUARD:
        chains = list(_AUDIT_CHAINS.values())
        _AUDIT_CHAINS.clear()
    for chain in chains:
        try:
            if chain.path.is_file():
                chain.seal()
        except Exception:
            logger.warning("failed to seal audit chain: %s", chain.path, exc_info=True)
        finally:
            chain.close()


class AuditLog:
    """Append-only, tamper-evident JSONL audit sink.

    Every entry is hash-chained (``seq``/``prev_hash``/``hash``) so editing,
    reordering, or deleting a prior entry is detectable via :meth:`verify`.
    The chain head is anchored (``<log>.head``) on :meth:`close` and at server
    shutdown (:func:`close_audit_chains`), so a rollback to a
    shorter-but-consistent chain is also detected.  ``record`` runs synchronously
    from the guard on the event loop; chaining adds only SHA-256 hashing, not
    extra fsync, on top of the existing per-append flush.

    The chain itself is shared per log path across every instance in the process
    (see :data:`_AUDIT_CHAINS`) — one guard per request must not mean one
    independent view of a shared append-only file.
    """

    _CHAIN_NAMESPACE = "agent-smith-audit"

    def __init__(self, log_path: Optional[Path] = None):
        if log_path is None:
            try:
                from common.config import DATA_DIR
                log_path = DATA_DIR / "audit.jsonl"
            except Exception:
                log_path = Path.home() / ".agent-smith" / "audit.jsonl"
        self._path = log_path
        self._chain: HashChainLog | None = None

    def _ensure_chain(self) -> HashChainLog:
        """Return the shared chain for the current path."""
        if self._chain is None or self._chain.path != self._path:
            # The previous chain is process-shared and may still be in use by a
            # concurrent request, so it is never closed here.
            self._chain = _shared_audit_chain(self._path, self._CHAIN_NAMESPACE)
        return self._chain

    @property
    def _handle(self):
        """The persistent append handle, opened lazily (test-facing seam)."""
        chain = self._ensure_chain()
        chain.ensure_handle()
        return chain.file_handle

    def record(self, tool_name: str, arguments: dict, result: GuardResult, **extra: object) -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "tool": tool_name,
            "args_summary": _summarize_args(arguments),
            "allowed": result.allowed,
            "level": result.level.value,
            "risk": result.risk.value,
            "reason": result.reason or None,
            **_summarize_args(extra),
        }
        try:
            # No per-run sealing: the log is install-wide and concurrent runs
            # interleave in it, so a run boundary is not a chain head.  An anchor
            # written there is stale as soon as another run appends, and would
            # itself read as an ``anchor mismatch``.  Sealing happens on
            # :meth:`close` and at shutdown (``close_audit_chains``), which are
            # the only boundaries where the head is well defined.
            self._ensure_chain().append(entry)
        except Exception:
            logger.warning("failed to append tool safety audit", exc_info=True)

    def verify(self) -> ChainVerification:
        """Walk the chain and report the first integrity failure."""
        anchor = None
        anchor_path = self._path.with_name(self._path.name + ".head")
        if anchor_path.is_file():
            try:
                anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                anchor = None
        return verify_chain(
            self._path,
            namespace=self._CHAIN_NAMESPACE,
            anchor=anchor if isinstance(anchor, dict) else None,
        )

    def seal(self) -> None:
        """Anchor the chain head at the current path."""
        self._ensure_chain().seal()

    def flush(self) -> None:
        chain = self._ensure_chain()
        if chain.file_handle is not None:
            chain.file_handle.flush()

    def close(self) -> None:
        if self._chain is not None:
            try:
                if self._path.is_file():
                    self._chain.seal()
            finally:
                self._chain.close()
                self._chain = None


def _summarize_sequence(values: list | tuple, max_len: int) -> list[object]:
    """Summarize a sequence, redacting flag-value pairs and secret-shaped items.

    Mirrors ``approval._summarize_list`` so the audit trail stays as clean as
    the approval card: a positional ``["--token", "sk-..."]`` pair would
    otherwise round-trip the credential verbatim into the log.
    """
    redacted: list[object] = []
    redact_next = False
    for item in values:
        if redact_next:
            redacted.append("***")
            redact_next = False
            continue
        if isinstance(item, str):
            match = _SECRET_FLAG_RE.match(item)
            if match:
                if "=" in item:
                    redacted.append(item.split("=", 1)[0] + "=***")
                else:
                    redacted.append(item)
                    redact_next = True
                continue
            if _is_secret_value(item):
                redacted.append("***")
                continue
        redacted.append(_summarize_value(item, max_len))
    return redacted


def _summarize_value(value: object, max_len: int) -> object:
    if isinstance(value, dict):
        return _summarize_args(value, max_len=max_len)
    if isinstance(value, (list, tuple)):
        return _summarize_sequence(value, max_len)
    if isinstance(value, str):
        # Mirror approval._summarize_value: a credential embedded in an
        # otherwise-innocuous string (a shell command carrying a bearer token,
        # a URL with userinfo) must be redacted before it lands in the
        # persistent audit trail.  Whole-string credentials are hidden
        # entirely by _redact_secret_text.
        safe = _redact_secret_text(value)
        if len(safe) > max_len:
            return safe[:max_len] + f"...({len(value)} chars)"
        return safe
    return value


def _summarize_args(args: dict, max_len: int = 200) -> dict:
    """Recursively redact credential-bearing argument fields for audit storage."""
    redacted = {}
    for k, v in args.items():
        if _is_sensitive_argument_name(k):
            redacted[k] = "***"
        else:
            redacted[k] = _summarize_value(v, max_len)
    return redacted


# ── Session whitelist (req #5: session-scoped overrides) ────

class SessionWhitelist:
    def __init__(self) -> None:
        self._allowed_tools: set[str] = set()
        self._allowed_paths: set[str] = set()
        self._allowed_files: set[str] = set()

    def allow_tool(self, tool_name: str) -> None:
        self._allowed_tools.add(tool_name)

    def allow_path(self, path: str) -> None:
        self._allowed_paths.add(str(Path(path).resolve()))

    def allow_file(self, path: str) -> None:
        self._allowed_files.add(str(Path(path).resolve()))

    def is_tool_allowed(self, tool_name: str) -> bool:
        return tool_name in self._allowed_tools

    def is_path_allowed(self, path: str) -> bool:
        try:
            resolved = Path(path).resolve()
        except (ValueError, OSError):
            return False
        if str(resolved) in self._allowed_files:
            return True
        for p in self._allowed_paths:
            base = Path(p)
            try:
                if resolved == base or resolved.is_relative_to(base):
                    return True
            except ValueError:
                continue
        return False

    def clear(self) -> None:
        self._allowed_tools.clear()
        self._allowed_paths.clear()
        self._allowed_files.clear()


# ── Shell path extraction (req #4: redirects + pipes) ───────

_REDIRECT_RE = re.compile(r"(?:>>?|[12]>>?|&>>?)\s*([^\s;|&]+)")

_PLATFORM_DATA_ROOT = (Path.home() / ".agent-smith").resolve()
_MEMORY_WRITE_ROOT = _PLATFORM_DATA_ROOT / "agent" / "memory"
_MEMORY_WRITE_FILES = frozenset({"recent.jsonl", "recent.md", "durable.md"})
_RUNTIME_CREDENTIAL_PATHS = frozenset(runtime_credential_paths(_PLATFORM_DATA_ROOT))


def _is_runtime_credential_path(*paths: Path) -> bool:
    """Whether a model-facing path would expose a provider/runtime credential."""
    folded_secrets = {_casefolded(path) for path in _RUNTIME_CREDENTIAL_PATHS}
    return any(_casefolded(path) in folded_secrets for path in paths)


def _shares_inode_with_runtime_credential(target: Path) -> bool:
    """Reject a hard-link alias of an Agent runtime credential file.

    ``Path.resolve`` catches symlinks but intentionally cannot distinguish a
    second name for the same inode. The small, explicit credential set makes
    this comparison cheap enough for every model-facing file read and write.
    """
    try:
        target_info = os.lstat(target)
    except OSError:
        return False
    if target_info.st_nlink < 2 or not os.path.isfile(target):
        return False
    identity = (target_info.st_dev, target_info.st_ino)
    for credential_path in _RUNTIME_CREDENTIAL_PATHS:
        try:
            credential_info = os.lstat(credential_path)
        except OSError:
            continue
        if (credential_info.st_dev, credential_info.st_ino) == identity:
            return True
    return False


def _command_mentions_runtime_credential(command: str) -> bool:
    """Catch the literal shell spellings of non-delegable runtime config files.

    Shell is intentionally opaque, so this helper is only an early diagnostic;
    the Seatbelt profile independently denies the resolved runtime-secret paths
    for variable expansion and other forms this text check cannot parse.
    """
    home = str(Path.home())
    aliases: set[str] = set()
    for path in _RUNTIME_CREDENTIAL_PATHS:
        rendered = str(path)
        aliases.add(rendered)
        if rendered.startswith(home):
            suffix = rendered[len(home):]
            aliases.add(f"~{suffix}")
            aliases.add(f"$HOME{suffix}")
            aliases.add(f"${{HOME}}{suffix}")
    return any(alias in command for alias in aliases)


def _extract_shell_write_paths(command: str) -> list[str]:
    """Extract literal redirect targets from a raw shell command.

    Only writes are extracted.  A shell command's read set cannot be derived
    from a regex, so the boundary for reads is the user approval that every
    shell call already requires — not a partial pattern match.
    """
    return [m.strip("'\"") for m in _REDIRECT_RE.findall(command)]


def _rule_match_targets(arguments: dict) -> list[str]:
    """Strings that dangerous-command rule patterns are matched against.

    The JSON dump alone breaks ``$``-anchored patterns (every value in the
    dump is followed by ``"``), so each raw string value is matched as well.
    """
    targets: list[str] = [json.dumps(arguments, ensure_ascii=False)]
    stack: list[object] = list(arguments.values())
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        elif isinstance(current, str):
            targets.append(current)
    return targets


def call_requests_network_egress(
    tool_call: ToolCall,
    definition: ToolDefinition | None,
) -> bool:
    """True when a tool call opens external network connections."""
    if definition is not None and getattr(definition, "network_access", False):
        return True
    if definition is not None and definition.opaque_command:
        return tool_call.arguments.get("network") is True
    return False


# ── Main guard ──────────────────────────────────────────────

class ToolGuard:
    def __init__(
        self,
        rules_path: Path,
        allowed_dirs: list[Path] | None = None,
        *,
        tool_registry: dict[str, ToolDefinition] | None = None,
    ) -> None:
        self._rules: list[dict] = []
        if rules_path.is_file():
            # Validate the shape here rather than letting every later check()
            # raise: a mis-edited rules file must fail loudly at load time, not
            # turn each agent run into an opaque failure.
            parsed = json.loads(rules_path.read_text(encoding="utf-8"))
            if not isinstance(parsed, list) or not all(
                isinstance(rule, dict) for rule in parsed
            ):
                raise ValueError(
                    f"dangerous-command rules must be a list of objects: {rules_path}"
                )
            self._rules = parsed
        self.file_guard = FileGuard(allowed_dirs)
        self.audit = AuditLog()
        self.whitelist = SessionWhitelist()
        self._tool_registry: dict[str, ToolDefinition] = tool_registry or {}
        self._sandbox_is_degraded: bool = False
        self._sandbox_allows_host_loopback: bool = False
        self._safety_config: SafetyConfig = SafetyConfig()
        self._taint_store: SessionTaintStore | None = None
        self._vault_store: VaultStore | None = None
        self._repeat_guard: RepeatSensitiveChecker = RepeatSensitiveChecker(
            self._safety_config.repeat_guard
        )
        self._subagent_mode: bool = False

    def set_sandbox_degraded(self, degraded: bool) -> None:
        """Mark whether the bound sandbox is running in degraded host fallback mode."""
        self._sandbox_is_degraded = bool(degraded)

    def set_sandbox_host_loopback(self, enabled: bool) -> None:
        """Mark whether the sandbox can reach host 127.0.0.1 (e.g. macOS loopback_only)."""
        self._sandbox_allows_host_loopback = bool(enabled)

    def configure_safety(
        self,
        safety_config: SafetyConfig | None = None,
        *,
        taint_store: SessionTaintStore | None = None,
        vault_store: VaultStore | None = None,
        subagent_mode: bool = False,
    ) -> None:
        """Attach declarative rules, session taint store, and vault store."""
        if safety_config is not None:
            self._safety_config = safety_config
            self._repeat_guard = RepeatSensitiveChecker(safety_config.repeat_guard)
        if taint_store is not None:
            self._taint_store = taint_store
        if vault_store is not None:
            self._vault_store = vault_store
        self._subagent_mode = bool(subagent_mode)

    @property
    def taint_store(self) -> SessionTaintStore | None:
        return self._taint_store

    @property
    def safety_config(self) -> SafetyConfig:
        return self._safety_config

    @property
    def vault_store(self) -> VaultStore | None:
        return self._vault_store

    def fork_for_subagent(self) -> tuple[ToolGuard, SessionTaintStore]:
        """Return a subagent-scoped guard copy and an isolated taint collector.

        Subagents inherit the parent's current taint state (read-only snapshot)
        and enforce hard-deny for taint egress and repeat sensitive calls.
        Any new taint sources recorded during the subagent run stay in the
        returned collector for monotonic upward merge into the parent store.
        """
        import copy as _copy

        child_taint = SessionTaintStore()
        if self._taint_store is not None:
            child_taint.merge_from(self._taint_store.sources)
        child = _copy.copy(self)
        child._taint_store = child_taint
        child._repeat_guard = RepeatSensitiveChecker(self._safety_config.repeat_guard)
        child._subagent_mode = True
        return child, child_taint

    def bind_definitions(self, definitions: dict[str, ToolDefinition]) -> None:
        """Bind tool definitions after registry load so metadata-first checks apply."""
        self._tool_registry = definitions

    def set_allowed_dirs(self, allowed_dirs: list[Path]) -> None:
        """Replace the file boundary for the current request's project root."""
        self.file_guard.set_allowed_dirs(allowed_dirs)

    def allow_project_instruction_path(self, project_root: Path) -> Path:
        """Whitelist only the canonical project instruction file for an explicit /init action."""
        target = project_root.resolve() / ".smith" / "SMITH.md"
        self.whitelist.allow_file(str(target))
        return target

    def set_working_directory(
        self,
        working_dir: Path,
        *,
        extra_roots: Iterable[Path] = (),
    ) -> None:
        """Constrain project-facing file tools to one resolved workspace (plus extra_roots)."""
        self.file_guard.set_working_directory(working_dir, extra_roots=extra_roots)

    def set_non_delegable_write_roots(self, roots: list[Path]) -> None:
        """Block model writes to runtime-owned executable provider roots."""
        self.file_guard.set_non_delegable_write_roots(roots)

    def _resolve_path_metadata(
        self,
        tool_name: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...], bool, bool]:
        """Return path and opaque-command metadata for a registered tool."""
        defn = self._tool_registry.get(tool_name)
        if defn is None:
            return (), (), False, False
        return defn.path_args, defn.list_path_args, defn.is_write_tool, defn.opaque_command

    def _path_arg_uses_nonempty_default(self, tool_name: str, arg_name: str) -> bool:
        """Whether an omitted path would make the provider use an implicit path."""
        definition = self._tool_registry.get(tool_name)
        if definition is not None and isinstance(definition.parameters, dict):
            properties = definition.parameters.get("properties")
            parameter = properties.get(arg_name) if isinstance(properties, dict) else None
            default = parameter.get("default") if isinstance(parameter, dict) else None
            return isinstance(default, str) and bool(default)
        return False

    def _check_file_paths(self, tool_call: ToolCall) -> GuardResult | None:
        path_args, list_path_args, is_write, opaque_command = self._resolve_path_metadata(tool_call.name)
        if not path_args and not list_path_args and not opaque_command:
            return None

        # (path, writing, declared_path) — declared_path is the caller's own
        # spelling for scalar path args (see FileGuard.check_path); list args and
        # shell redirect targets are never pre-resolved, so they have none.
        paths_to_check: list[tuple[str, bool, str | None]] = []

        for arg_name in path_args:
            path_val = tool_call.arguments.get(arg_name)
            if path_val is None or path_val == "":
                if (
                    self.file_guard.is_working_directory_scoped
                    and self._path_arg_uses_nonempty_default(tool_call.name, arg_name)
                ):
                    # The registry must first materialize the provider's default
                    # (such as ``path='.'``) within the workspace.  Letting a raw
                    # optional path through here would make the provider use the
                    # server process CWD instead.
                    return GuardResult(
                        allowed=False,
                        reason=(
                            f"Path argument '{arg_name}' must be canonicalized "
                            "before policy checks"
                        ),
                        boundary_block=True,
                    )
                continue
            if not isinstance(path_val, str):
                # A scalar path argument must be a single string.  A list or
                # dict here would be str()-stringified into one bogus path the
                # guard checks, while the provider reads the real (possibly
                # sensitive) value differently — a check/execute divergence.
                # Reject rather than approximate.
                return GuardResult(
                    allowed=False,
                    reason=(
                        f"Path argument '{arg_name}' must be a string, "
                        f"got {type(path_val).__name__}"
                    ),
                )
            paths_to_check.append(
                (path_val, is_write, tool_call.declared_paths.get(arg_name))
            )

        cwd_val = tool_call.arguments.get("cwd")
        cwd = str(cwd_val) if cwd_val else ""

        for arg_name in list_path_args:
            raw_values = tool_call.arguments.get(arg_name) or []
            if not isinstance(raw_values, list):
                continue
            for raw in raw_values:
                p = Path(str(raw))
                if cwd and not p.is_absolute():
                    p = Path(cwd) / p
                paths_to_check.append((str(p), is_write, None))

        if opaque_command:
            cmd = tool_call.arguments.get("command", "")
            definition = self._tool_registry.get(tool_call.name)
            requires_sandbox = (
                definition is not None
                and definition.execution_environment == "sandbox"
            )
            if self.file_guard.is_working_directory_scoped and not requires_sandbox:
                return GuardResult(
                    allowed=False,
                    reason=(
                        "Opaque command execution requires a sandbox while a "
                        "working-directory boundary is active"
                    ),
                )
            write_paths = _extract_shell_write_paths(cmd)
            for wp in write_paths:
                # A raw shell command cannot be safely parsed into a complete
                # filesystem access list.  It is therefore always approved by
                # the user (see the shell metadata), rather than pretending a
                # partial regex enforces the project boundary. Preserve a
                # high-risk approval for literal runtime-state writes; provider
                # credential files remain non-delegable in FileGuard itself.
                try:
                    candidate = Path(wp).expanduser().resolve()
                except (ValueError, OSError):
                    continue
                if candidate.is_relative_to(_PLATFORM_DATA_ROOT):
                    paths_to_check.append((wp, True, None))

        for p, writing, declared in paths_to_check:
            result = self.file_guard.check_path(
                p, writing=writing, declared_path=declared
            )
            if not result.allowed:
                # The session whitelist may extend only the ordinary directory
                # boundary; high-risk paths do not set ``boundary_block``.
                if result.boundary_block and self.whitelist.is_path_allowed(p):
                    continue
                return result
        return None

    def _resolve_permission_level(self, tool_name: str) -> PermissionLevel:
        """Return the permission level declared by the registered tool.

        Unknown or unregistered tools are treated as host execution. This is
        intentionally conservative and avoids a second, name-based security
        registry drifting away from the provider metadata.
        """
        defn = self._tool_registry.get(tool_name)
        if defn is not None and defn.permission_level:
            try:
                return PermissionLevel(defn.permission_level)
            except ValueError:
                pass
        return PermissionLevel.EXECUTE

    def _resolve_approval_policy(self, tool_name: str) -> str:
        """Resolve approval requirements from registered tool metadata."""
        defn = self._tool_registry.get(tool_name)
        if defn is None:
            return "always"
        if defn.approval_policy != "never":
            return defn.approval_policy
        if (
            defn.is_write_tool
            or defn.side_effect != "none"
            or defn.permission_level in {"write", "execute", "destructive"}
        ):
            return "policy"
        return "never"

    def _requires_approval(self, tool_call: ToolCall) -> bool:
        definition = self._tool_registry.get(tool_call.name)
        if definition is not None and getattr(definition, "network_access", False):
            return True
        policy = self._resolve_approval_policy(tool_call.name)
        if policy == "never":
            return False
        if policy == "always":
            return True
        action = str(tool_call.arguments.get("action") or "").lower()
        if definition is not None and action in definition.read_actions:
            return False
        return True

    def _approval_scope_for(
        self,
        tool_call: ToolCall,
        *,
        high_risk: bool = False,
    ) -> ApprovalScope:
        """Describe the smallest capability that can execute this call.

        This is deliberately derived from provider metadata rather than a
        second table of tool names. Opaque shell text is not safely parseable,
        so its one approved command receives an explicit host-command scope.
        """
        path_args, list_path_args, is_write, opaque_command = self._resolve_path_metadata(
            tool_call.name
        )
        if opaque_command:
            cmd = str(tool_call.arguments.get("command") or "")
            if self._sandbox_is_degraded:
                return ApprovalScope.host_command(cmd, high_risk=True)
            if tool_call.arguments.get("network") is True:
                return ApprovalScope.sandbox_network(cmd, high_risk=True)
            return ApprovalScope.sandbox_command(cmd, high_risk=high_risk)

        definition = self._tool_registry.get(tool_call.name)
        if definition is not None and getattr(definition, "network_access", False):
            target = (
                tool_call.arguments.get("url")
                or tool_call.arguments.get("query")
                or tool_call.arguments.get("target")
                or tool_call.name
            )
            return ApprovalScope.network(str(target), high_risk=high_risk)

        # A multi-action tool's capability is the action, not its path arguments.
        # ``git_ops`` declares ``cwd`` as a path arg and ``normalize_call``
        # always fills it with the workspace, so every action — including
        # ``push``, which sends code to a remote — rendered as the same
        # "write to <workspace>" card, describing neither what would happen nor
        # where.  Naming the action keeps the approval honest.
        action = str(tool_call.arguments.get("action") or "").strip()
        if action:
            return ApprovalScope.operation(
                f"{tool_call.name}:{action}", high_risk=high_risk
            )

        for argument_name in path_args:
            value = tool_call.arguments.get(argument_name)
            if value:
                return ApprovalScope.path(
                    str(value),
                    writing=is_write,
                    high_risk=high_risk,
                )
        for argument_name in list_path_args:
            values = tool_call.arguments.get(argument_name)
            if isinstance(values, list) and values:
                return ApprovalScope.path(
                    str(values[0]),
                    writing=is_write,
                    high_risk=high_risk,
                )
        return ApprovalScope.operation(tool_call.name, high_risk=high_risk)

    def _check_dangerous_rules(self, tool_call: ToolCall) -> GuardResult | None:
        match_targets = _rule_match_targets(tool_call.arguments)
        for rule in self._rules:
            scoped_tools = rule.get("tools")
            if scoped_tools and tool_call.name not in scoped_tools:
                continue

            patterns = rule.get("patterns", [])
            single = rule.get("pattern", "")
            if single:
                patterns = [single] + list(patterns)

            exclude_patterns = rule.get("excludePatterns", [])

            for pattern in patterns:
                if not pattern:
                    continue
                for target in match_targets:
                    if not re.search(pattern, target):
                        continue
                    if any(ep and re.search(ep, target) for ep in exclude_patterns):
                        continue
                    reason = rule.get("reason") or rule.get("description") or f"Blocked: {pattern}"
                    return GuardResult(
                        allowed=False,
                        reason=(
                            f"[{rule.get('id', '?')}] {reason}; high-risk approval "
                            "is required"
                        ),
                        level=PermissionLevel.DESTRUCTIVE,
                        needs_confirmation=True,
                        approval_required=True,
                        approval_scope=self._approval_scope_for(tool_call, high_risk=True),
                        risk=RiskTier.CRITICAL,
                        rule_id=str(rule.get("id") or "") or None,
                    )
        return None

    def _classify_call_bucket(
        self,
        tool_call: ToolCall,
        definition: ToolDefinition | None,
        *,
        file_result: GuardResult | None,
        dangerous_hit: bool,
        egress_target: EgressTarget | None,
        tainted: bool,
        risk: RiskTier,
    ) -> tuple[BucketKey, str] | None:
        is_sensitive_path_hit = bool(
            file_result is not None
            and file_result.approval_required
            and not file_result.boundary_block
        )
        is_outside_write = bool(
            file_result is not None
            and file_result.boundary_block
            and definition is not None
            and definition.is_write_tool
        )
        is_host_or_dangerous = bool(
            dangerous_hit
            or (
                definition is not None
                and definition.opaque_command
                and self._sandbox_is_degraded
            )
        )
        return classify_sensitive_bucket(
            tool_call,
            definition,
            is_sensitive_path=is_sensitive_path_hit,
            is_outside_write=is_outside_write,
            is_host_or_dangerous=is_host_or_dangerous,
            egress_target=egress_target,
            tainted=tainted,
            risk=risk,
            min_risk=self._safety_config.repeat_guard.min_risk,
        )

    def check(self, tool_call: ToolCall, *, audit: bool = True) -> GuardResult:
        """Evaluate one call against the L0–L7 decision matrix.

        ``audit=False`` suppresses the audit record for a re-check of a call
        that was already evaluated and logged.  The registry runs this guard a
        second time as an execution backstop; without this the audit trail
        would report twice the number of tool calls that actually happened.
        """
        level = self._resolve_permission_level(tool_call.name)
        tool_whitelisted = self.whitelist.is_tool_allowed(tool_call.name)
        definition = self._tool_registry.get(tool_call.name)
        is_opaque = bool(self._resolve_path_metadata(tool_call.name)[3])
        is_tainted = bool(
            self._safety_config.taint.enabled
            and self._taint_store is not None
            and self._taint_store.is_tainted
        )
        taint_labels = self._taint_store.labels() if is_tainted and self._taint_store is not None else ()
        egress_target = resolve_egress_target(
            tool_call,
            definition,
            working_dir=self.file_guard._working_dir,
            sandbox_is_degraded=self._sandbox_is_degraded,
            sandbox_allows_host_loopback=self._sandbox_allows_host_loopback,
        )
        vault_keys = extract_vault_placeholders(tool_call.arguments)

        approval_context = current_approval_context()
        audit_context: dict[str, object] = {"call_id": tool_call.id}
        if approval_context is not None:
            audit_context["run_id"] = approval_context[1]
        if is_tainted:
            audit_context["tainted"] = True
        if egress_target is not None:
            audit_context["egress_target"] = egress_target.target
        if vault_keys:
            audit_context["vault_keys"] = vault_keys

        def record(result: GuardResult, **extra: object) -> None:
            if audit:
                if result.rule_id and "rule_id" not in extra:
                    extra["rule_id"] = result.rule_id
                if result.repeat_count >= 2:
                    extra["repeat_count"] = result.repeat_count
                self.audit.record(
                    tool_call.name,
                    tool_call.arguments,
                    result,
                    **extra,
                    **audit_context,
                )

        # ── L0: Non-bypassable hard floor ──
        if is_opaque and _command_mentions_runtime_credential(
            str(tool_call.arguments.get("command") or "")
        ):
            result = GuardResult(
                allowed=False,
                reason=(
                    "[runtime-secret-001] Agent runtime credential files are not "
                    "delegable to model tools"
                ),
                level=level,
                risk=RiskTier.CRITICAL,
            )
            record(result)
            return result

        if vault_keys:
            if is_opaque:
                result = GuardResult(
                    allowed=False,
                    reason=(
                        "[vault-shell-001] Vault placeholders ({{vault:...}}) are "
                        "forbidden in opaque shell commands"
                    ),
                    level=level,
                    risk=RiskTier.CRITICAL,
                )
                record(result)
                return result
            if definition is not None and not definition.accepts_vault_placeholders:
                result = GuardResult(
                    allowed=False,
                    reason=(
                        f"[vault-shell-001] Tool '{tool_call.name}' does not accept "
                        "vault placeholders ({{vault:...}})"
                    ),
                    level=level,
                    risk=RiskTier.CRITICAL,
                )
                record(result)
                return result
            if self._vault_store is not None:
                target_domain = (
                    egress_target.target
                    if egress_target is not None and egress_target.kind == "http_host"
                    else None
                )
                ok_vault, vault_err, _ = self._vault_store.validate_placeholders(
                    tool_call.name,
                    tool_call.arguments,
                    target_domain=target_domain,
                )
                if not ok_vault:
                    result = GuardResult(
                        allowed=False,
                        reason=vault_err,
                        level=level,
                        risk=RiskTier.CRITICAL,
                    )
                    record(result)
                    return result

        if is_opaque and "network" in tool_call.arguments and not isinstance(
            tool_call.arguments["network"], bool
        ):
            bad_net = GuardResult(
                allowed=False,
                level=level,
                reason="Argument 'network' must be a boolean",
                risk=RiskTier.HIGH,
            )
            record(bad_net)
            return bad_net

        file_result = self._check_file_paths(tool_call)
        if file_result is not None and not file_result.allowed and not file_result.approval_required:
            file_result.level = level
            file_result.risk = self._risk_for_file_result(file_result)
            record(file_result)
            return file_result

        # ── L1: Declarative rules with action: deny ──
        deny_rule = self._safety_config.match_deny(tool_call, tainted=is_tainted)
        if deny_rule is not None:
            result = GuardResult(
                allowed=False,
                reason=f"[{deny_rule.id}] {deny_rule.reason}",
                level=level,
                risk=deny_rule.risk,
                rule_id=deny_rule.id,
                taint_sources=taint_labels,
            )
            record(result)
            return result

        # ── L2: Session taint egress gate ──
        taint_ask_result: GuardResult | None = None
        if is_tainted and egress_target is not None:
            if not self._safety_config.taint.allowlist.allows(egress_target):
                sources_desc = ", ".join(taint_labels)
                reason = (
                    f"[taint-egress-001] Outbound egress to '{egress_target.raw_display}' "
                    f"blocked/escalated because this session read private data ({sources_desc})"
                )
                if self._subagent_mode or self._safety_config.taint.on_egress == "deny":
                    result = GuardResult(
                        allowed=False,
                        reason=reason,
                        level=level,
                        risk=RiskTier.CRITICAL,
                        taint_sources=taint_labels,
                    )
                    record(result)
                    return result
                if self._safety_config.taint.on_egress == "ask":
                    taint_ask_result = GuardResult(
                        allowed=False,
                        reason=reason,
                        level=level,
                        needs_confirmation=True,
                        approval_required=True,
                        approval_scope=self._approval_scope_for(tool_call, high_risk=True),
                        risk=RiskTier.CRITICAL,
                        taint_sources=taint_labels,
                    )

        # Match non-deny declarative rule (L5) early so L3/L4 know whether allow applies
        matched_rule = self._safety_config.match_non_deny(tool_call, tainted=is_tainted)

        # ── L3: Built-in dangerous rules & FileGuard high-risk paths ──
        dangerous_result = self._check_dangerous_rules(tool_call)
        candidate: GuardResult | None = None
        dangerous_hit = dangerous_result is not None

        if taint_ask_result is not None:
            candidate = taint_ask_result
        elif dangerous_result is not None:
            candidate = dangerous_result
        elif file_result is not None:
            file_result.level = level
            file_result.risk = self._risk_for_file_result(file_result)
            if file_result.boundary_block:
                # L4: outside-workspace boundary can be waived by an explicit allow rule
                # when the session is not tainted.
                if (
                    matched_rule is not None
                    and matched_rule.action == "allow"
                    and not is_tainted
                ):
                    candidate = GuardResult(
                        allowed=True,
                        level=level,
                        risk=RiskTier.ROUTINE,
                        rule_id=matched_rule.id,
                    )
                else:
                    candidate = file_result
            else:
                # L3 high-risk sensitive path: only override if explicitly allowed by
                # a non-CRITICAL override_builtin_approval rule when clean.
                if (
                    matched_rule is not None
                    and matched_rule.action == "allow"
                    and matched_rule.override_builtin_approval
                    and not is_tainted
                    and file_result.risk is not RiskTier.CRITICAL
                ):
                    candidate = GuardResult(
                        allowed=True,
                        level=level,
                        risk=RiskTier.ROUTINE,
                        rule_id=matched_rule.id,
                    )
                else:
                    candidate = file_result

        # ── L5 & L6: Declarative ask/allow + default tool approval policy ──
        if candidate is None:
            if matched_rule is not None and matched_rule.action == "ask":
                high = matched_rule.risk >= RiskTier.HIGH
                candidate = GuardResult(
                    allowed=False,
                    level=level,
                    reason=f"[{matched_rule.id}] {matched_rule.reason}",
                    needs_confirmation=True,
                    approval_required=True,
                    approval_scope=self._approval_scope_for(tool_call, high_risk=high),
                    risk=matched_rule.risk,
                    rule_id=matched_rule.id,
                    taint_sources=taint_labels if is_tainted else (),
                )
            elif matched_rule is not None and matched_rule.action == "allow":
                candidate = GuardResult(
                    allowed=True,
                    level=level,
                    reason="",
                    approval_required=False,
                    approval_scope=None,
                    risk=RiskTier.ROUTINE,
                    rule_id=matched_rule.id,
                )
            else:
                approval_required = self._requires_approval(tool_call)
                approval_scope = (
                    self._approval_scope_for(tool_call) if approval_required else None
                )
                network_access = call_requests_network_egress(tool_call, definition)
                if is_opaque and self._sandbox_is_degraded:
                    reason = (
                        f"[NO OS SANDBOX] Approval required for {tool_call.name} "
                        "(running in explicit host_with_approval fallback mode)"
                    )
                    risk = RiskTier.CRITICAL
                elif is_opaque and network_access:
                    reason = (
                        f"Outbound network approval required for {tool_call.name} "
                        "(filesystem sandbox remains active)"
                    )
                    risk = RiskTier.HIGH
                else:
                    reason = (
                        f"Approval required for {tool_call.name}"
                        if approval_required
                        else ""
                    )
                    risk = (
                        risk_for_approval(
                            level=level.value,
                            high_risk=bool(
                                approval_scope is not None and approval_scope.high_risk
                            ),
                            network_access=network_access,
                        )
                        if approval_required
                        else RiskTier.ROUTINE
                    )
                candidate = GuardResult(
                    allowed=True,
                    level=level,
                    reason=reason,
                    approval_required=approval_required,
                    approval_scope=approval_scope,
                    risk=risk,
                )

        # ── L7: Repeat sensitive call guard (cannot be bypassed by allow/whitelist) ──
        bucket_info = self._classify_call_bucket(
            tool_call,
            definition,
            file_result=file_result,
            dangerous_hit=dangerous_hit,
            egress_target=egress_target,
            tainted=is_tainted,
            risk=candidate.risk,
        )
        if bucket_info is not None:
            bucket_key, bucket_label = bucket_info
            candidate.repeat_bucket = bucket_key
            triggered, next_count = self._repeat_guard.should_trigger(bucket_key)
            if triggered:
                repeat_reason = (
                    f"[repeat-sensitive-001] Sensitive operation ({bucket_label}) "
                    f"attempt #{next_count} in this run requires explicit confirmation"
                )
                if (
                    self._subagent_mode
                    or self._safety_config.repeat_guard.on_trigger == "deny"
                ):
                    candidate = GuardResult(
                        allowed=False,
                        reason=repeat_reason,
                        level=candidate.level,
                        needs_confirmation=False,
                        approval_required=False,
                        risk=RiskTier.CRITICAL,
                        rule_id=candidate.rule_id,
                        taint_sources=candidate.taint_sources or (taint_labels if is_tainted else ()),
                        repeat_count=next_count,
                        repeat_bucket=bucket_key,
                    )
                else:
                    candidate = GuardResult(
                        allowed=False,
                        reason=repeat_reason,
                        level=candidate.level,
                        needs_confirmation=True,
                        approval_required=True,
                        approval_scope=self._approval_scope_for(
                            tool_call, high_risk=True
                        ),
                        risk=RiskTier.CRITICAL,
                        rule_id=candidate.rule_id,
                        taint_sources=candidate.taint_sources or (taint_labels if is_tainted else ()),
                        repeat_count=next_count,
                        repeat_bucket=bucket_key,
                    )

        record(
            candidate,
            whitelisted=tool_whitelisted,
            sandbox_backend="host_degraded" if (is_opaque and self._sandbox_is_degraded) else None,
        )
        return candidate

    def repeat_guard_headroom(self, tool_call: ToolCall) -> tuple[BucketKey, int] | None:
        """Return ``(bucket_key, remaining_before_trigger)`` if RepeatGuard tracks this call.

        Read-only parallel batching calls this before queueing another call in
        the same bucket so a batch can never slip past ``repeat_guard.threshold``
        before ``record_post_execution`` increments the real counter.
        """
        if not self._safety_config.repeat_guard.enabled:
            return None
        definition = self._tool_registry.get(tool_call.name)
        file_result = self._check_file_paths(tool_call)
        dangerous_hit = self._check_dangerous_rules(tool_call) is not None
        egress_target = resolve_egress_target(
            tool_call,
            definition,
            working_dir=self.file_guard._working_dir,
            sandbox_is_degraded=self._sandbox_is_degraded,
            sandbox_allows_host_loopback=self._sandbox_allows_host_loopback,
        )
        is_tainted = bool(
            self._safety_config.taint.enabled
            and self._taint_store is not None
            and self._taint_store.is_tainted
        )
        eval_risk = RiskTier.ROUTINE
        if file_result is not None:
            eval_risk = self._risk_for_file_result(file_result)
        elif dangerous_hit or (
            definition is not None
            and definition.opaque_command
            and self._sandbox_is_degraded
        ):
            eval_risk = RiskTier.CRITICAL
        elif call_requests_network_egress(tool_call, definition):
            eval_risk = RiskTier.HIGH

        bucket_info = self._classify_call_bucket(
            tool_call,
            definition,
            file_result=file_result,
            dangerous_hit=dangerous_hit,
            egress_target=egress_target,
            tainted=is_tainted,
            risk=eval_risk,
        )
        if bucket_info is None:
            return None
        bucket_key = bucket_info[0]
        current = self._repeat_guard.current_count(bucket_key)
        headroom = max(0, self._safety_config.repeat_guard.threshold - 1 - current)
        return bucket_key, headroom

    def record_post_execution(
        self,
        tool_call: ToolCall,
        result: ToolResult,
        *,
        run_id: str = "",
    ) -> None:
        """Update repeat sensitive counters and session taint immediately after execution.

        Taint and secret detection ALWAYS run on executed calls — even when
        ``result.is_error`` is True (for example a non-zero exit code such as
        ``cat prod.yaml; false`` or content starting with ``Error:``).  Only
        unexecuted policy/registry rejections are skipped, and repeat-guard
        counters still only increment on non-error executions.
        """
        if result.error_kind in {
            "policy_blocked",
            "approval_required",
            "unknown_tool",
            "tool_disabled",
            "environment_unavailable",
        }:
            return
        definition = self._tool_registry.get(tool_call.name)

        # 1. Update repeat sensitive counter for semantic bucket (only on success)
        if not result.is_error:
            file_result = self._check_file_paths(tool_call)
            dangerous_hit = self._check_dangerous_rules(tool_call) is not None
            egress_target = resolve_egress_target(
                tool_call,
                definition,
                working_dir=self.file_guard._working_dir,
                sandbox_is_degraded=self._sandbox_is_degraded,
                sandbox_allows_host_loopback=self._sandbox_allows_host_loopback,
            )
            was_tainted = bool(
                self._taint_store is not None and self._taint_store.is_tainted
            )
            eval_risk = RiskTier.ROUTINE
            if file_result is not None:
                eval_risk = self._risk_for_file_result(file_result)
            elif dangerous_hit or (
                definition is not None
                and definition.opaque_command
                and self._sandbox_is_degraded
            ):
                eval_risk = RiskTier.CRITICAL
            elif call_requests_network_egress(tool_call, definition):
                eval_risk = RiskTier.HIGH

            bucket_info = self._classify_call_bucket(
                tool_call,
                definition,
                file_result=file_result,
                dangerous_hit=dangerous_hit,
                egress_target=egress_target,
                tainted=was_tainted,
                risk=eval_risk,
            )
            if bucket_info is not None:
                self._repeat_guard.record(bucket_info[0])

        # 2. Update SessionTaintStore immediately (before next tool_call in same turn!),
        #    regardless of whether result.is_error is True.
        if self._taint_store is None or not self._safety_config.taint.enabled:
            return

        # 2a. Static private-data tool (e.g. memory_ops search, configured private MCP)
        if definition is not None and definition.reads_private_data:
            action = str(tool_call.arguments.get("action") or "").lower()
            if tool_call.name != "memory_ops" or action in {"", "search"}:
                label = (
                    f"mcp:{tool_call.name}"
                    if tool_call.name.startswith("mcp_")
                    else f"tool:{tool_call.name}"
                )
                self._taint_store.record(
                    "private_tool", tool_call.name, label, run_id=run_id
                )

        # 2b. Sensitive file read or outside-workspace read
        if definition is not None and not definition.is_write_tool and definition.path_args:
            for arg_name in definition.path_args:
                raw_path = tool_call.arguments.get(arg_name)
                if not isinstance(raw_path, str) or not raw_path:
                    continue
                p = Path(raw_path).expanduser()
                try:
                    resolved = p.resolve()
                except OSError:
                    resolved = p
                if is_sensitive_path(resolved) or is_sensitive_path(p):
                    self._taint_store.record(
                        "sensitive_file",
                        tool_call.name,
                        f"file:{resolved.name or p.name}",
                        run_id=run_id,
                    )
                elif (
                    self.file_guard.is_working_directory_scoped
                    and not self.file_guard.is_inside_allowed_dirs(resolved)
                ):
                    self._taint_store.record(
                        "outside_workspace",
                        tool_call.name,
                        f"outside:{resolved.name or str(resolved)}",
                        run_id=run_id,
                    )

        # 2c. Host-level shell execution
        if definition is not None and definition.opaque_command:
            if (
                self._sandbox_is_degraded
                or result.metadata.get("sandbox_backend") == "host_degraded"
                or result.metadata.get("sandbox_backend") == "host"
            ):
                self._taint_store.record(
                    "host_shell", tool_call.name, "host:shell", run_id=run_id
                )

        # 2d. Output contained high-confidence secret or triggered vault redaction
        if result.metadata.get("vault_redacted") or detect_secret_in_text(result.content):
            self._taint_store.record(
                "secret_output",
                tool_call.name,
                f"secret:{tool_call.name}",
                run_id=run_id,
            )

    @staticmethod
    def _risk_for_file_result(result: GuardResult) -> RiskTier:
        """Derive the risk tier of a path-check result for the approval flow.

        Hard denials carry no approval flow, but the tier still records why the
        call was refused in the audit trail.
        """
        if result.approval_required:
            return risk_for_approval(
                level=result.level.value,
                high_risk=bool(
                    result.approval_scope is not None and result.approval_scope.high_risk
                ),
            )
        if result.level is PermissionLevel.DESTRUCTIVE or any(
            marker in (result.reason or "")
            for marker in ("runtime-secret", "unsafe-alias", "runtime-provider")
        ):
            return RiskTier.CRITICAL
        return RiskTier.ROUTINE
