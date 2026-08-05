"""Grep tool — search file contents using ripgrep (rg) or fallback to grep."""

import asyncio
import functools
import os
import subprocess

TOOL_META = {
    "name": "grep",
    "description": (
        "Search file contents for a pattern. Uses ripgrep (rg) if available, "
        "falls back to grep. Returns matching file paths with line numbers and context. "
        "Automatically skips .git, node_modules, __pycache__, .venv, and binary files."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Search pattern (regex supported)"},
            "path": {"type": "string", "description": "Directory or file to search in", "default": "."},
            "include": {"type": "string", "description": "File glob filter, e.g. '*.py'"},
            "ignore_case": {"type": "boolean", "default": False},
            "context_lines": {"type": "integer", "description": "Lines of context (0-5)", "default": 0},
            "files_only": {"type": "boolean", "description": "Only list matching file paths", "default": False},
        },
        "required": ["pattern"],
    },
    "path_args": ["path"],
    "permission_level": "read",
    "approval_policy": "never",
    "side_effect": "none",
    "execution_environment": "host",
}

MAX_RESULTS = 200
MAX_LINE_LEN = 500
EXCLUDED = [".git", "node_modules", "__pycache__", ".venv", "dist", ".build", "*.egg-info"]
# Credential-bearing paths a *directory* walk must never return.  ToolGuard
# reasons about the path the model names; this tool names a directory and
# returns the contents of everything beneath it, so read_file's sensitive-file
# gate does not cover the subtree and the provider has to exclude these itself.
# Naming such a file directly stays possible — the guard sees that basename and
# gates it — so this list only narrows the recursive case.
SECRET_EXCLUDED = [
    ".ssh", ".gnupg", ".aws", ".kube",              # credential directories
    ".env*", ".npmrc", ".pypirc", ".netrc",         # credential files
    "*.pem", "*.key", "*.p12", "*.pfx",             # private keys and certs
    # SSH keys, including copies affixed away from ~/.ssh (id_rsa_old,
    # backup-id_ed25519).  ToolGuard._is_sensitive_read_name gates these with a
    # word-boundary regex, so a bare-basename exclusion here let a directory
    # walk dump exactly the affixed keys the guard blocks on a direct read.
    # The globs over-match slightly (grid_rsa) — harmless: an over-excluded file
    # is still searchable when named directly, where the guard then governs it.
    "*id_rsa*", "*id_dsa*", "*id_ecdsa*", "*id_ed25519*",
]
_SAFE_ENV_KEYS = ("LANG", "LC_ALL", "TERM", "TZ", "NO_COLOR")


def _casefold_glob(pattern: str) -> str:
    """Make a single-component glob case-insensitive via character classes.

    GNU/BSD grep's ``--exclude-dir`` matches case-sensitively, unlike the
    ripgrep ``--iglob`` used in the primary path.  Rewriting each letter as a
    ``[xX]`` class makes ``.git`` exclude ``.GIT``/``.Git`` the same way the
    runtime's own ``_casefolded()`` discipline does elsewhere.
    """
    out: list[str] = []
    for char in pattern:
        if char.isalpha():
            out.append(f"[{char.lower()}{char.upper()}]")
        else:
            out.append(char)
    return "".join(out)


def _safe_environment(search_path: str) -> dict[str, str]:
    """Minimal env for the search subprocess, mirroring shell/git_ops.

    rg and grep inherit the server process environment by default; stripping
    it keeps service credentials and user-level config out of a model-requested
    search.  ``RIPGREP_CONFIG_PATH`` is pinned to the null device so a user's
    global rg config cannot silently change search semantics.
    """
    environment = {
        "PATH": os.environ.get("PATH") or os.defpath,
        "HOME": os.path.abspath(search_path),
        "RIPGREP_CONFIG_PATH": os.devnull,
    }
    for key in _SAFE_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment


@functools.lru_cache(maxsize=1)
def _has_rg() -> bool:
    try:
        subprocess.run(["rg", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _execute_sync(
    *, pattern: str, path: str = ".", include: str = "",
    ignore_case: bool = False, context_lines: int = 0, files_only: bool = False,
) -> str:
    include = include or None
    if not pattern.strip():
        return "Error: pattern is required"
    resolved = os.path.abspath(path)
    if not os.path.exists(resolved):
        return f"Error: path not found: {resolved}"

    # Secrets are excluded only when walking a directory.  A file the model
    # names is a different case: ToolGuard sees that basename and gates it, so
    # excluding it here would block a search the user already approved.
    secret_globs = SECRET_EXCLUDED if os.path.isdir(resolved) else []

    use_rg = _has_rg()
    if use_rg:
        args = ["rg", "--hidden", "--max-columns", str(MAX_LINE_LEN), "--max-count", "50"]
        for e in EXCLUDED + secret_globs:
            # --iglob, not --glob: on a case-insensitive filesystem the exclusion
            # list should suppress the directory however it happens to be typed.
            args.extend(["--iglob", f"!{e}"])
        if ignore_case: args.append("-i")
        if files_only: args.append("-l")
        else:
            args.append("-n")
            if context_lines > 0: args.extend(["-C", str(min(context_lines, 5))])
        if include: args.extend(["--glob", include])
        args.extend(["-e", pattern] if pattern.startswith("-") else [pattern])
        args.append(resolved)
    else:
        # -E is not optional.  Without it BSD/GNU grep defaults to POSIX BRE,
        # where `|` is a literal character, so an alternation like `cat|dog`
        # reports "no matches" against text containing both — a confidently
        # wrong answer, in a dialect other than the one ripgrep (this tool's
        # primary engine) would have used.
        args = ["grep", "-r", "-E", "--binary-files=without-match"]
        for e in EXCLUDED:
            args.extend(["--exclude-dir", _casefold_glob(e)])
        for e in secret_globs:
            # --exclude-dir skips a matching directory, --exclude a matching
            # file; the secret list holds both kinds, so every entry gets both.
            folded = _casefold_glob(e)
            args.extend(["--exclude-dir", folded, "--exclude", folded])
        if ignore_case: args.append("-i")
        if files_only: args.append("-l")
        else:
            args.append("-n")
            if context_lines > 0: args.extend(["-C", str(min(context_lines, 5))])
        if include: args.extend(["--include", include])
        args.extend(["-e", pattern] if pattern.startswith("-") else [pattern])
        args.append(resolved)

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
            env=_safe_environment(resolved),
        )
    except subprocess.TimeoutExpired:
        return "Error: search timed out. Try a more specific pattern or path."

    # Both engines use exit 1 for "ran fine, matched nothing" and >=2 for a real
    # failure.  Reading only stdout made an invalid pattern indistinguishable
    # from an empty result set, so a rejected search read as "not in the codebase".
    if result.returncode >= 2:
        detail = (result.stderr or "").strip().splitlines()
        reason = detail[0][:300] if detail else f"search exited with code {result.returncode}"
        return f"Error: search failed: {reason}"

    output = result.stdout.strip()
    if not output:
        return f"No matches found for: {pattern}"

    engine = "rg" if use_rg else "grep -E"
    lines = output.split("\n")
    total = len(lines)
    lines = [l[:MAX_LINE_LEN] + "…" if len(l) > MAX_LINE_LEN else l for l in lines[:MAX_RESULTS]]
    header = f"# grep ({engine}): {min(total, MAX_RESULTS)} results"
    if total > MAX_RESULTS:
        header += f" (showing {MAX_RESULTS} of {total})"
    return header + "\n" + "\n".join(lines)


async def execute(
    *, pattern: str, path: str = ".", include: str = "",
    ignore_case: bool = False, context_lines: int = 0, files_only: bool = False,
) -> str:
    return await asyncio.to_thread(
        _execute_sync,
        pattern=pattern,
        path=path,
        include=include,
        ignore_case=ignore_case,
        context_lines=context_lines,
        files_only=files_only,
    )
