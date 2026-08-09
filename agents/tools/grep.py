"""Grep tool — search file contents using ripgrep (rg) or fallback to grep."""
# 优先使用 rg，并主动跳过常见生成目录与二进制文件以控制噪声和开销。

import asyncio
import fnmatch
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
            "include": {
                "type": "string",
                "description": (
                    "File glob filter, e.g. '*.py'. A filter that could match a credential "
                    "file (such as '*' or '*.pem') is rejected when searching a directory."
                ),
            },
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
# Kept in sync by hand with ``engine.safety.tool_guard._CREDENTIAL_CONFIG_NAMES``
# / ``FileGuard._ALWAYS_BLOCKED`` and ``engine.sandbox.macos_seatbelt``'s
# credential sets.  ``agents/`` may not import the engine, so the duplication is
# structural — but the three copies had drifted, and the weakest one set the
# real security level.  Change all three together.
SECRET_EXCLUDED = [
    ".ssh", ".gnupg", ".aws", ".kube", ".docker",   # credential directories
    ".env*", ".npmrc", ".pypirc", ".netrc",         # credential files
    ".git-credentials",                             # git credential store
    "*.pem", "*.key", "*.p12", "*.pfx",             # private keys and certs
    # SSH keys outside ~/.ssh, including copies and rotations that keep the
    # shape but not the exact name (id_rsa_old, backup-id_ed25519).  Exact
    # names alone let grep return a private key that read_file refuses without
    # high-risk approval; keep aligned with the same widening in
    # engine/safety/tool_guard.py and engine/sandbox/macos_seatbelt.py.
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "*[-_.]id_rsa*", "*[-_.]id_dsa*", "*[-_.]id_ecdsa*", "*[-_.]id_ed25519*",
]
# A caller-supplied file filter WINS over the exclusions above in both engines:
# BSD/GNU grep applies --include before --exclude, and ripgrep gives precedence
# to the last matching glob (ours are emitted first).  So `include='*'` — or any
# glob that can name an excluded file — turned SECRET_EXCLUDED off entirely and
# returned .env / *.pem / .npmrc contents from a plain directory search, with no
# approval anywhere in the path.  Reordering the flags cannot fix this: grep's
# precedence is order-independent.  Reject such a filter instead.
#
# Only the FILE entries above need this.  --exclude-dir is not overridden by
# --include, so the credential directories stay protected either way.
_SECRET_PROBE_NAMES = (
    ".env", ".env.local", ".env.production",
    ".npmrc", ".pypirc", ".netrc", ".git-credentials",
    "server.pem", "server.key", "cert.p12", "cert.pfx",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    # Copies and rotations must be probed too, or a filter naming one of them
    # would slip past the reachability check that guards SECRET_EXCLUDED.
    "id_rsa_old", "backup-id_ed25519",
)
_SAFE_ENV_KEYS = ("LANG", "LC_ALL", "TERM", "TZ", "NO_COLOR")


def _include_reaches_secret(include: str) -> bool:
    """Whether a caller's file filter can name a SECRET_EXCLUDED file.

    Matched case-insensitively because both engines exclude case-insensitively
    (``--iglob`` / ``_casefold_glob``); a case-sensitive test here would let
    ``*.PEM`` through to reach ``SERVER.PEM``.  grep matches ``--include``
    against the base name while ripgrep matches the whole path, so the pattern's
    last component is probed as well.
    """
    lowered = include.lower()
    tail = lowered.replace("\\", "/").rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatchcase(name, candidate)
        for name in _SECRET_PROBE_NAMES
        for candidate in {lowered, tail}
    )


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
        # A timeout is not optional: this probe runs before every search, and a
        # wedged binary on PATH would hang the tool with no deadline of its own.
        subprocess.run(["rg", "--version"], capture_output=True, timeout=5, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _execute_sync(
    *, pattern: str, path: str = ".", include: str = "",
    ignore_case: bool = False, context_lines: int = 0, files_only: bool = False,
) -> str:
    # Argument types are validated before use: every one of these reaches either
    # a string method or an `argv` list, so a wrong type raised deep inside the
    # provider and surfaced as an opaque exception instead of a usable message.
    if not isinstance(pattern, str) or not pattern.strip():
        return "Error: pattern is required and must be a string"
    if not isinstance(path, str):
        return "Error: path must be a string"
    if include is not None and not isinstance(include, str):
        return "Error: include must be a string"
    if not isinstance(ignore_case, bool) or not isinstance(files_only, bool):
        return "Error: ignore_case and files_only must be booleans"
    if isinstance(context_lines, bool) or not isinstance(context_lines, int):
        return "Error: context_lines must be an integer"

    include = include or None
    resolved = os.path.abspath(path)
    if not os.path.exists(resolved):
        return f"Error: path not found: {resolved}"

    # Secrets are excluded only when walking a directory.  A file the model
    # names is a different case: ToolGuard sees that basename and gates it, so
    # excluding it here would block a search the user already approved.
    secret_globs = SECRET_EXCLUDED if os.path.isdir(resolved) else []

    if secret_globs and include and _include_reaches_secret(include):
        return (
            f"Error: the include filter '{include}' can match credential files "
            "that a directory search must never return. Narrow it to the file "
            "types you actually need (for example '*.py'), or name the single "
            "file directly as 'path' so the approval flow can gate it."
        )

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
