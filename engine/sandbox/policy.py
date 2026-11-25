"""Sandbox path-safety policy backed by ``common.sensitive_rules``.

Deliberately imports nothing from ``engine.safety`` to keep sandbox and safety
decoupled while consuming the exact same sensitive-rule SSoT.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from common.sensitive_rules import (
    CREDENTIAL_CONFIG_FILES,
    CREDENTIAL_DIRS,
    CREDENTIAL_PATH_PAIRS,
    PRIVATE_KEY_EXTENSIONS,
    SAFE_AGENT_SMITH_RO_REL_DIRS,
    SANDBOX_EXTRA_CREDENTIAL_DIRS,
    SSH_KEY_NAME_POSIX_ERE,
    SSH_KEY_NAME_RE,
    build_sbpl_sensitive_deny_clauses,
    is_hardlink_protected_path,
    is_sensitive_path,
    runtime_credential_paths,
)


def _casefolded(path: Path) -> Path:
    return Path(str(path).lower())


def has_control_characters(path: str | Path) -> bool:
    """Return True if a path contains ASCII control characters or DEL."""
    text = str(path)
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def assert_no_control_characters(path: str | Path, *, label: str = "sandbox path") -> None:
    if has_control_characters(path):
        raise ValueError(f"{label} contains control characters")


def _forbidden_broad_roots(home: Path, data_dir: Path) -> tuple[Path, ...]:
    candidates = [
        Path("/"),
        home,
        Path("/etc"),
        Path("/var"),
        Path("/private/etc"),
        Path("/private/var"),
        home / ".config",
        home / "Library",
        data_dir,
        data_dir / "agent",
    ]
    resolved: list[Path] = []
    for item in candidates:
        try:
            resolved.append(item.resolve())
        except OSError:
            resolved.append(item)
    return tuple(resolved)


def _is_safe_agent_smith_ro_dir(path: Path, data_dir: Path) -> bool:
    """Allow narrow read-only skill directories under ``~/.agent-smith``."""
    try:
        resolved_data = data_dir.resolve()
        folded_target = _casefolded(path.resolve())
    except OSError:
        return False
    for rel_parts in SAFE_AGENT_SMITH_RO_REL_DIRS:
        allowed = _casefolded(resolved_data.joinpath(*rel_parts))
        try:
            if folded_target == allowed or folded_target.is_relative_to(allowed):
                return True
        except ValueError:
            continue
    return False


def _overlaps_any(path: Path, protected: Iterable[Path]) -> Path | None:
    """Return the first protected path that equals, contains, or lies within ``path``."""
    folded_path = _casefolded(path)
    for item in protected:
        folded_item = _casefolded(item)
        try:
            if (
                folded_path == folded_item
                or folded_path.is_relative_to(folded_item)
                or folded_item.is_relative_to(folded_path)
            ):
                return item
        except ValueError:
            continue
    return None


def validate_configured_root(
    raw_path: str | Path,
    *,
    role: str,
    runtime_secret_paths: Iterable[Path] = (),
    non_delegable_write_paths: Iterable[Path] = (),
    home: Path | None = None,
    data_dir: Path | None = None,
) -> tuple[Path | None, str | None]:
    """Validate and resolve a configured sandbox directory root.

    ``role`` is one of ``"extra_root"``, ``"share_read_only"``, or ``"cache_root"``.
    Raises ``ValueError`` on control characters (fail-closed); returns
    ``(None, reason)`` when a path is rejected for security or missing-directory
    reasons so the resolver can record diagnostics cleanly.
    """
    assert_no_control_characters(raw_path, label=f"sandbox {role} path")
    resolved_home = (home if home is not None else Path.home()).expanduser().resolve()
    resolved_data = (
        data_dir if data_dir is not None else resolved_home / ".agent-smith"
    ).expanduser().resolve()
    secrets = tuple(
        Path(p).expanduser().resolve()
        for p in (runtime_secret_paths or runtime_credential_paths(resolved_data))
    )

    expanded = Path(raw_path).expanduser()
    try:
        resolved = expanded.resolve()
    except OSError as exc:
        return None, f"{role} path cannot be resolved ({raw_path}): {exc}"

    assert_no_control_characters(resolved, label=f"sandbox {role} path")

    # Reject forbidden overly-broad roots (e.g., /, $HOME, ~/.config, ~/.agent-smith)
    folded_resolved = _casefolded(resolved)
    for forbidden in _forbidden_broad_roots(resolved_home, resolved_data):
        if folded_resolved == _casefolded(forbidden):
            return (
                None,
                f"[SANDBOX_CONFIG_PATH_REJECTED] {role} cannot mount broad root: {resolved}",
            )

    # Check both lexical (expanded) and resolved views so symlinked credential
    # directories cannot masquerade as benign paths.
    allow_skill_subdir = role == "share_read_only" and _is_safe_agent_smith_ro_dir(
        resolved, resolved_data
    )
    for view in (expanded, resolved):
        if allow_skill_subdir and view == resolved:
            continue
        if allow_skill_subdir and _is_safe_agent_smith_ro_dir(view, resolved_data):
            continue
        if is_hardlink_protected_path(view):
            return (
                None,
                f"[SANDBOX_CONFIG_PATH_REJECTED] {role} touches protected credential path: {raw_path}",
            )

    # Reject any root that contains or equals a runtime secret file
    secret_hit = _overlaps_any(resolved, secrets)
    if secret_hit is not None:
        return (
            None,
            f"[SANDBOX_CONFIG_PATH_REJECTED] {role} overlaps runtime secret {secret_hit}: {resolved}",
        )

    # Cache roots must never overlap non-delegable executable provider roots
    if role == "cache_root":
        non_delegable_hit = _overlaps_any(
            resolved,
            (Path(p).expanduser().resolve() for p in non_delegable_write_paths),
        )
        if non_delegable_hit is not None:
            return (
                None,
                f"[SANDBOX_CONFIG_PATH_REJECTED] cache_root overlaps non-delegable path {non_delegable_hit}: {resolved}",
            )

    if not resolved.exists():
        return None, f"{role} does not exist and was skipped: {resolved}"
    if not resolved.is_dir():
        return None, f"{role} is not a directory and was skipped: {resolved}"
    return resolved, None


__all__ = (
    "CREDENTIAL_CONFIG_FILES",
    "CREDENTIAL_DIRS",
    "CREDENTIAL_PATH_PAIRS",
    "PRIVATE_KEY_EXTENSIONS",
    "SANDBOX_EXTRA_CREDENTIAL_DIRS",
    "SSH_KEY_NAME_POSIX_ERE",
    "SSH_KEY_NAME_RE",
    "assert_no_control_characters",
    "build_sbpl_sensitive_deny_clauses",
    "has_control_characters",
    "is_hardlink_protected_path",
    "is_sensitive_path",
    "runtime_credential_paths",
    "validate_configured_root",
)
