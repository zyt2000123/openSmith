"""Single source of truth for sensitive path and credential filename rules.

This module intentionally uses only the Python standard library so that
``engine.safety``, ``engine.sandbox``, and runtime tool-binding helpers can all
consume the exact same definitions without introducing cross-layer cycles.
``agents/`` tools remain zero-import and receive a plain-data snapshot exported
by :func:`export_tool_rule_bundle` (with parity tests verifying their built-in
fallback constants never drift).
"""

from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path

# 1. Credential directories (single path component, case-insensitive)
CREDENTIAL_DIRS: frozenset[str] = frozenset({
    ".ssh",
    ".gnupg",
    ".aws",
    ".kube",
    ".docker",
})

# Sandbox profiles also treat `.agent-smith` inside a workspace as sensitive.
SANDBOX_EXTRA_CREDENTIAL_DIRS: frozenset[str] = frozenset({".agent-smith"})

# 2. Credential directory pairs (parent, child — both lowercase)
CREDENTIAL_PATH_PAIRS: frozenset[tuple[str, str]] = frozenset({
    (".config", "gh"),
    (".config", "gcloud"),
    ("library", "keychains"),
})

# 3. Credential config basenames (exact lowercase match)
CREDENTIAL_CONFIG_FILES: frozenset[str] = frozenset({
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
})

# 4. Sensitive filenames on write (in addition to `.env*` prefix matching)
SENSITIVE_WRITE_FILES: frozenset[str] = frozenset({
    ".env",
    ".env.local",
    ".env.production",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
})

# 5. Private key extensions and benign env templates
PRIVATE_KEY_EXTENSIONS: tuple[str, ...] = (".pem", ".key", ".p12", ".pfx")
ENV_TEMPLATE_SUFFIXES: tuple[str, ...] = (".env.example", ".env.template", ".env.sample")

# 6. Files inside `.git/` that embed or store credentials
GIT_CREDENTIAL_FILES: frozenset[str] = frozenset({
    "config",
    "config.worktree",
    "credentials",
})

# 7. SSH private key basename shape — written as POSIX ERE so the exact same
# pattern is compiled by both macOS Seatbelt regcomp and Python `re`.
SSH_KEY_NAME_POSIX_ERE: str = r"([^/]*[-_.])?id_(rsa|dsa|ecdsa|ed25519)([-_.][^/]*)?"
SSH_KEY_NAME_RE: re.Pattern[str] = re.compile(
    rf"^{SSH_KEY_NAME_POSIX_ERE}$",
    re.IGNORECASE,
)

# 8. Agent-Smith runtime credential files (relative to `~/.agent-smith`) that
# are never delegable to model tools or sandboxed commands.
RUNTIME_CREDENTIAL_REL_PATHS: tuple[tuple[str, ...], ...] = (
    ("config.yaml",),
    ("config.yml",),
    ("agent", "config.yaml"),
    ("agent", "config.yml"),
    ("vault.json",),
    ("auth_token",),
)

# Relative directories under `~/.agent-smith` that hold read-only skill assets
# and are safe to expose read-only inside sandboxes without exposing runtime secrets.
SAFE_AGENT_SMITH_RO_REL_DIRS: tuple[tuple[str, ...], ...] = (
    ("builtin", "skills"),
    ("agent", "skills"),
)


def runtime_credential_paths(data_dir: Path | None = None) -> tuple[Path, ...]:
    """Return resolved non-delegable runtime credential paths under ``data_dir``."""
    base = (data_dir if data_dir is not None else Path.home() / ".agent-smith").expanduser()
    try:
        resolved_base = base.resolve()
    except OSError:
        resolved_base = base
    seen: dict[Path, None] = {}
    for rel_parts in RUNTIME_CREDENTIAL_REL_PATHS:
        candidate = resolved_base.joinpath(*rel_parts)
        try:
            candidate = candidate.resolve()
        except OSError:
            pass
        seen.setdefault(candidate, None)
    return tuple(seen)


def match_credential_pair(path: Path) -> str | None:
    """Return the ``parent/child`` credential directory entered by ``path``, if any."""
    folded = tuple(part.lower() for part in path.parts)
    for parent, child in pairwise(folded):
        if (parent, child) in CREDENTIAL_PATH_PAIRS:
            return f"{parent}/{child}"
    return None


def is_sensitive_read_name(name: str) -> bool:
    """Whether a basename warrants high-risk approval / exclusion on read."""
    folded = name.lower()
    if folded.endswith(".pub"):
        return False
    if folded.startswith(".env"):
        return not folded.endswith(ENV_TEMPLATE_SUFFIXES)
    if folded in CREDENTIAL_CONFIG_FILES:
        return True
    if folded.endswith(PRIVATE_KEY_EXTENSIONS):
        return True
    return SSH_KEY_NAME_RE.fullmatch(folded) is not None


def is_sensitive_write_name(name: str) -> bool:
    """Whether a basename is sensitive when written."""
    folded = name.lower()
    return folded in SENSITIVE_WRITE_FILES or folded.startswith(".env")


def is_sensitive_path(
    path: Path,
    *,
    include_sandbox_extra: bool = False,
    include_env_templates: bool = False,
) -> bool:
    """Whether ``path`` points into a credential directory or sensitive file."""
    lowered_parts = tuple(part.lower() for part in path.parts)
    dirs = (
        CREDENTIAL_DIRS | SANDBOX_EXTRA_CREDENTIAL_DIRS
        if include_sandbox_extra
        else CREDENTIAL_DIRS
    )
    if any(part in dirs for part in lowered_parts):
        return True
    if match_credential_pair(path) is not None:
        return True
    name = path.name.lower()
    if not name:
        return False
    if include_env_templates and (name == ".env" or name.startswith(".env.")):
        return True
    if is_sensitive_read_name(name):
        return True
    if name in GIT_CREDENTIAL_FILES and ".git" in lowered_parts:
        return True
    return False


def is_hardlink_protected_path(path: Path) -> bool:
    """Whether ``path`` is protected against sandbox hardlink aliasing."""
    lowered_parts = tuple(part.lower() for part in path.parts)
    return (
        is_sensitive_path(
            path,
            include_sandbox_extra=True,
            include_env_templates=True,
        )
        or ".git" in lowered_parts
    )


def derive_grep_secret_globs() -> tuple[str, ...]:
    """Derive directory/file exclusion globs for ``grep`` directory walks."""
    globs: list[str] = [
        *sorted(CREDENTIAL_DIRS),
        ".env*",
        *sorted(CREDENTIAL_CONFIG_FILES),
        *(f"*{ext}" for ext in PRIVATE_KEY_EXTENSIONS),
        "id_rsa*",
        "id_dsa*",
        "id_ecdsa*",
        "id_ed25519*",
        "*[-_.]id_rsa*",
        "*[-_.]id_dsa*",
        "*[-_.]id_ecdsa*",
        "*[-_.]id_ed25519*",
    ]
    return tuple(globs)


def derive_grep_secret_pair_globs() -> tuple[str, ...]:
    """Derive multi-component credential pair globs for ripgrep walks."""
    return tuple(
        f"{parent}/{child}/**"
        for parent, child in sorted(CREDENTIAL_PATH_PAIRS)
    )


def derive_secret_probe_names() -> tuple[str, ...]:
    """Sample filenames probed against user-supplied grep ``include`` globs."""
    return (
        ".env",
        ".env.local",
        ".env.production",
        *sorted(CREDENTIAL_CONFIG_FILES),
        *(f"secret{ext}" for ext in PRIVATE_KEY_EXTENSIONS),
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa_old",
        "backup-id_ed25519",
    )


def build_sbpl_sensitive_deny_clauses(root_param: str = "WORKSPACE") -> str:
    """Build macOS Seatbelt deny rules scoped to ``(param "<root_param>")``."""
    dir_alternation = "|".join(
        part.lstrip(".")
        for part in sorted(CREDENTIAL_DIRS | SANDBOX_EXTRA_CREDENTIAL_DIRS)
    )
    config_alternation = "|".join(
        name.lstrip(".") for name in sorted(CREDENTIAL_CONFIG_FILES)
    )
    ext_alternation = "|".join(ext.lstrip(".") for ext in PRIVATE_KEY_EXTENSIONS)
    return (
        f'(deny file-read* file-write*\n'
        f'    (regex (string-append "^" (regex-quote (param "{root_param}"))\n'
        f'        #"/(.*/)?\\.env($|\\..*)")))\n'
        f'(deny file-read* file-write*\n'
        f'    (regex (string-append "^" (regex-quote (param "{root_param}"))\n'
        f'        #"/(.*/)?\\.({dir_alternation})(/|$)")))\n'
        f'(deny file-read* file-write*\n'
        f'    (regex (string-append "^" (regex-quote (param "{root_param}"))\n'
        f'        #"/(.*/)?\\.config/(gh|gcloud)(/|$)")))\n'
        f'(deny file-read* file-write*\n'
        f'    (regex (string-append "^" (regex-quote (param "{root_param}"))\n'
        f'        #"/(.*/)?Library/Keychains(/|$)")))\n'
        f'(deny file-read* file-write*\n'
        f'    (regex (string-append "^" (regex-quote (param "{root_param}"))\n'
        f'        #"/.*\\.({ext_alternation})$")))\n'
        f'(deny file-read* file-write*\n'
        f'    (regex (string-append "^" (regex-quote (param "{root_param}"))\n'
        f'        #"/(.*/)?{SSH_KEY_NAME_POSIX_ERE}$")))\n'
        f'(deny file-read* file-write*\n'
        f'    (regex (string-append "^" (regex-quote (param "{root_param}"))\n'
        f'        #"/(.*/)?\\.git/config$")))\n'
        f'(deny file-read* file-write*\n'
        f'    (regex (string-append "^" (regex-quote (param "{root_param}"))\n'
        f'        #"/(.*/)?\\.git/credentials$")))\n'
        f'(deny file-read* file-write*\n'
        f'    (regex (string-append "^" (regex-quote (param "{root_param}"))\n'
        f'        #"/(.*/)?\\.git(/|$)")))\n'
        f'(deny file-read* file-write*\n'
        f'    (regex (string-append "^" (regex-quote (param "{root_param}"))\n'
        f'        #"/(.*/)?\\.({config_alternation})$")))\n'
    )


def export_tool_rule_bundle() -> dict[str, object]:
    """Export plain built-in data for injection into zero-import ``agents/tools``."""
    return {
        "credential_dirs": CREDENTIAL_DIRS,
        "credential_path_pairs": CREDENTIAL_PATH_PAIRS,
        "credential_config_files": CREDENTIAL_CONFIG_FILES,
        "sensitive_write_files": SENSITIVE_WRITE_FILES,
        "private_key_extensions": PRIVATE_KEY_EXTENSIONS,
        "env_template_suffixes": ENV_TEMPLATE_SUFFIXES,
        "git_credential_files": GIT_CREDENTIAL_FILES,
        "ssh_key_pattern": SSH_KEY_NAME_POSIX_ERE,
        "grep_secret_globs": derive_grep_secret_globs(),
        "grep_secret_pair_globs": derive_grep_secret_pair_globs(),
        "probe_names": derive_secret_probe_names(),
    }


__all__ = (
    "CREDENTIAL_CONFIG_FILES",
    "CREDENTIAL_DIRS",
    "CREDENTIAL_PATH_PAIRS",
    "ENV_TEMPLATE_SUFFIXES",
    "GIT_CREDENTIAL_FILES",
    "PRIVATE_KEY_EXTENSIONS",
    "RUNTIME_CREDENTIAL_REL_PATHS",
    "SAFE_AGENT_SMITH_RO_REL_DIRS",
    "SANDBOX_EXTRA_CREDENTIAL_DIRS",
    "SENSITIVE_WRITE_FILES",
    "SSH_KEY_NAME_POSIX_ERE",
    "SSH_KEY_NAME_RE",
    "build_sbpl_sensitive_deny_clauses",
    "derive_grep_secret_globs",
    "derive_grep_secret_pair_globs",
    "derive_secret_probe_names",
    "export_tool_rule_bundle",
    "is_hardlink_protected_path",
    "is_sensitive_path",
    "is_sensitive_read_name",
    "is_sensitive_write_name",
    "match_credential_pair",
    "runtime_credential_paths",
)
