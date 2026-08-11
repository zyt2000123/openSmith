from __future__ import annotations

"""Git workflow tool provider — branch, commit, push, worktree operations.

Runs every git command through the injected execution environment in argv
mode (no shell interpretation). Validates inputs to prevent injection and
checks for sensitive files before staging.
"""
# Git 参数以 argv 传递而非 shell 拼接，并在暂存前拦截敏感文件。

import os
import re

TOOL_META = {
    "name": "git_ops",
    "description": "Git workflow operations: status, diff, branch, commit, push, worktree management, and repo discovery.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "status",
                    "diff",
                    "branch_create",
                    "commit",
                    "push",
                    "worktree_create",
                    "worktree_remove",
                    "discover",
                ],
                "description": "The git operation to perform",
            },
            "cwd": {
                "type": "string",
                "description": "Repository working directory (defaults to current directory)",
            },
            "branch": {
                "type": "string",
                "description": "Branch name (for branch_create, push)",
            },
            "message": {
                "type": "string",
                "description": "Commit message (for commit)",
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files to stage (for commit; omit to stage all tracked changes)",
            },
            "staged": {
                "type": "boolean",
                "description": "Show staged changes only (for diff, default false)",
                "default": False,
            },
            "path": {
                "type": "string",
                "description": "Worktree path (for worktree_remove)",
            },
        },
        "required": ["action"],
    },
    "path_args": ["cwd", "path"],
    "list_path_args": ["files"],
    "is_write_tool": True,
    "permission_level": "write",
    "approval_policy": "policy",
    "read_actions": ["status", "diff", "discover"],
    "side_effect": "external",
    "concurrency": "serial",
    "execution_environment": "host",
}

MAX_OUTPUT = 10 * 1024  # 10KB
_SAFE_ENV_KEYS = ("LANG", "LC_ALL", "TERM", "TZ", "NO_COLOR")

# Branch/tag name validation: alphanumeric, dash, underscore, dot, slash
_SAFE_REF = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,200}$")

# Patterns for sensitive files that should not be staged
_SENSITIVE_PATTERNS = re.compile(
    r"(?i)"
    r"(^|/)\.env($|\.)"
    r"|(^|/)credentials"
    r"|(^|/)secrets?"
    r"|(^|/).*\.pem$"
    r"|(^|/).*\.key$"
    r"|(^|/).*_rsa$"
    r"|(^|/).*_dsa$"
    r"|(^|/)\.aws/"
    r"|(^|/)\.ssh/"
    r"|(^|/)id_rsa"
    r"|(^|/)id_ed25519"
)


def _validate_ref(name: str) -> str | None:
    """Return an error message if ref name is unsafe, else None."""
    if not name:
        return "branch name is empty"
    if not _SAFE_REF.match(name):
        return f"branch name contains unsafe characters: {name!r}"
    if ".." in name or name.endswith(".lock"):
        return f"branch name is invalid: {name!r}"
    return None


def _safe_environment(cwd: str | None) -> dict[str, str]:
    """Return a minimal environment for model-requested Git processes.

    Git may execute repository-controlled hooks, filters, and helpers.  Those
    subprocesses must not inherit provider credentials or other service
    secrets owned by the Agent-Smith runtime.  ``GIT_PAGER``/``GIT_EDITOR``
    pin interactive programs to no-ops since our captures are pipes anyway.
    """
    home = os.path.abspath(cwd) if cwd else os.getcwd()
    environment = {
        "PATH": os.environ.get("PATH") or os.defpath,
        "HOME": home,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
        "GIT_PAGER": "cat",
        "GIT_EDITOR": "true",
    }
    for key in _SAFE_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment


def _host_config_environment(cwd: str | None) -> dict[str, str]:
    """The safe environment, but with the host's real git config stack visible.

    Reserved for read-only ``git config`` queries: config lookup executes no
    repository-controlled hooks, filters, or helpers, so it alone may see the
    user's real HOME (and any explicit GIT_CONFIG_* / XDG_CONFIG_HOME
    redirection) without exposing runtime secrets to repository code.
    """
    environment = _safe_environment(cwd)
    home = os.environ.get("HOME")
    if home:
        environment["HOME"] = home
    for key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "XDG_CONFIG_HOME"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
        else:
            environment.pop(key, None)
    return environment


async def _resolve_git_identity(
    repo_dir: str, environment
) -> tuple[str | None, str | None]:
    """Resolve the identity git itself would pick, honoring the host config stack.

    ``_safe_environment`` hides the user's global/system config from every git
    subprocess so repository-controlled code cannot read runtime secrets — but
    that also hid ``user.name``/``user.email``, so committing in a repo without
    a local identity either failed outright or let git fabricate ``user@fqdn``
    from the hostname.  Query the values read-only with the real stack visible
    (repo config still wins, matching git's own precedence, because the lookup
    runs inside the repository) and pin them onto ``commit`` explicitly.
    """
    values: list[str | None] = []
    env = _host_config_environment(repo_dir)
    for key in ("user.name", "user.email"):
        rc, out, _ = await _run_git(
            ["config", "--get", key], cwd=repo_dir, environment=environment, env=env
        )
        value = out.strip() if rc == 0 else ""
        values.append(value or None)
    return values[0], values[1]


async def _run_git(
    args: list[str],
    cwd: str | None = None,
    timeout: int = 30,
    environment=None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run a git command via the execution environment; return (returncode, stdout, stderr)."""
    if environment is None:
        return -1, "", "no execution environment is available for git"
    # A repository's .git/config is trusted input from the workspace and can
    # point git at commands it would then execute in this process.  Neutralize
    # every such knob we know about with command-line overrides (which beat
    # repo config): hooks, the fsmonitor helper, external diffs, credential
    # helpers, and a custom ssh transport.  Filters (clean/smudge via
    # .gitattributes) and remote.<name>.receivepack/uploadpack have no global
    # override and remain a documented residual.
    harden = [
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.fsmonitor=false",
        "-c", "diff.external=",
        "-c", "credential.helper=",
        "-c", "core.sshCommand=ssh",
    ]
    result = await environment.run_command(
        argv=["git", *harden, *args],
        cwd=cwd,
        timeout_seconds=timeout,
        env=env if env is not None else _safe_environment(cwd),
    )
    if result.timed_out:
        return -1, "", f"git command timed out after {timeout}s"
    if result.error:
        return -1, "", result.error
    exit_code = result.exit_code if result.exit_code is not None else -1
    return exit_code, result.stdout, result.stderr


_URL_CREDENTIALS_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/@\s]+)@")


def _redact_url_credentials(text: str) -> str:
    """Strip userinfo (including any embedded password) from URLs.

    A repository's remote URL can carry embedded credentials
    (``https://user:token@host/repo``); ``git remote -v`` and push output echo
    them verbatim, leaking the token into tool output and the transcript.
    """
    return _URL_CREDENTIALS_RE.sub(r"\1***@", text)


def _format_result(returncode: int, stdout: str, stderr: str) -> str:
    """Format git output into a single result string."""
    parts: list[str] = []
    if stdout:
        text = stdout if len(stdout) <= MAX_OUTPUT else stdout[:MAX_OUTPUT] + "\n... (truncated)"
        parts.append(_redact_url_credentials(text))
    if stderr:
        text = stderr if len(stderr) <= MAX_OUTPUT else stderr[:MAX_OUTPUT] + "\n... (truncated)"
        parts.append(f"[stderr]\n{_redact_url_credentials(text)}")
    body = "\n".join(parts) if parts else "(no output)"
    return f"[exit_code={returncode}]\n{body}"


def _check_sensitive_files(files: list[str]) -> list[str]:
    """Return list of sensitive file paths that should not be staged."""
    return [f for f in files if _SENSITIVE_PATTERNS.search(f)]


def _parse_add_dry_run(output: str) -> list[str]:
    """Extract the paths ``git add --dry-run`` reports it would stage.

    Git prints one ``add '<path>'`` line per file, so this is the only way to
    learn what a *pathspec* such as ``*`` or ``src/`` actually expands to —
    matching the argument strings against sensitive-name patterns tells us
    nothing about the files they reach.  ``remove '<path>'`` lines are ignored:
    deleting a file cannot publish its contents.

    A line that starts with ``add`` but does not parse is returned raw, so an
    unexpected format fails closed into the sensitive scan rather than out of it.
    """
    paths: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("add "):
            continue
        remainder = stripped[4:].strip()
        if len(remainder) >= 2 and remainder.startswith("'") and remainder.endswith("'"):
            paths.append(remainder[1:-1])
        else:
            paths.append(remainder)
    return paths


def _resolve_cwd(cwd: str | None) -> str:
    """Resolve working directory, default to current directory."""
    if cwd:
        if not os.path.isdir(cwd):
            return ""
        return cwd
    return os.getcwd()


async def execute(
    *,
    action: str,
    cwd: str | None = None,
    branch: str | None = None,
    message: str | None = None,
    files: list[str] | None = None,
    staged: bool = False,
    path: str | None = None,
    environment=None,
) -> str:
    repo_dir = _resolve_cwd(cwd)
    if not repo_dir:
        return f"Error: working directory does not exist: {cwd}"
    if environment is None:
        return "Error: no execution environment is available for git_ops"

    async def run(args: list[str], *, timeout: int = 30) -> tuple[int, str, str]:
        return await _run_git(args, cwd=repo_dir, timeout=timeout, environment=environment)

    # Verify we're in a git repo
    rc, _, _ = await run(["rev-parse", "--git-dir"])
    if rc != 0:
        return f"Error: {repo_dir} is not a git repository"

    if action == "status":
        rc, out, err = await run(["status", "--short", "--branch"])
        return _format_result(rc, out, err)

    elif action == "diff":
        args = ["diff"]
        if staged:
            args.append("--staged")
        args.append("--stat")
        rc_stat, out_stat, _ = await run(args)

        args_full = ["diff"]
        if staged:
            args_full.append("--staged")
        rc, out, err = await run(args_full)
        combined = f"{out_stat.rstrip()}\n\n{out}" if out_stat.strip() else out
        return _format_result(rc, combined, err)

    elif action == "branch_create":
        if not branch:
            return "Error: 'branch' is required for branch_create"
        err = _validate_ref(branch)
        if err:
            return f"Error: {err}"
        rc, out, err_msg = await run(["checkout", "-b", branch])
        return _format_result(rc, out, err_msg)

    elif action == "commit":
        if not message:
            return "Error: 'message' is required for commit"

        # Scan what this commit will actually contain, and do it BEFORE staging
        # anything so a refusal leaves the index untouched.  Two sources, both
        # required:
        #   1. the paths staging would add — obtained by letting git expand the
        #      pathspec itself, because ``files=["*"]`` or ``["src/"]`` reaches
        #      files whose names were never scanned;
        #   2. whatever is already staged — ``git commit -m`` commits the whole
        #      index, so a secret staged by any earlier action would ride along
        #      no matter how narrow this call's own file list looks.
        stage_args = ["add", "--"] + files if files else ["add", "-u"]
        rc_dry, dry_out, dry_err = await run(
            ["-c", "core.quotePath=false", *stage_args[:1], "--dry-run", *stage_args[1:]]
        )
        if rc_dry != 0:
            return _format_result(rc_dry, dry_out, dry_err)
        # -z, not core.quotePath=false: git C-quotes a path containing a `"` or
        # a `\` no matter how quotePath is set, and every _SENSITIVE_PATTERNS
        # branch anchors on `$` or `($|\.)`, so a trailing quote made them all
        # miss.  NUL-separated output is the only form that is never rewritten.
        rc_staged, staged_out, staged_err = await run(
            ["diff", "--name-only", "-z", "--diff-filter=ACMR", "--cached"]
        )
        # Fail closed: a scan that errored tells us nothing about the index, and
        # `git commit -m` would commit all of it.
        if rc_staged != 0:
            return _format_result(rc_staged, staged_out, staged_err)

        candidates = _parse_add_dry_run(dry_out)
        candidates.extend(path for path in staged_out.split("\0") if path)

        sensitive = _check_sensitive_files(candidates)
        if sensitive:
            return (
                f"Error: refusing to stage sensitive files: {', '.join(sorted(set(sensitive)))}. "
                f"Add them to .gitignore, unstage them, or name safe files explicitly."
            )

        rc, out, err_msg = await run(stage_args)
        if rc != 0:
            return _format_result(rc, out, err_msg)

        # Commit with the identity the user actually configured.  The isolated
        # environment hides the global config, so resolve it via a read-only
        # lookup and pin it explicitly; ``user.useConfigOnly`` keeps git from
        # fabricating a ``user@fqdn`` identity when nothing is configured —
        # failing with git's own actionable message instead.
        name, email = await _resolve_git_identity(repo_dir, environment)
        identity_args = ["-c", "user.useConfigOnly=true"]
        if name:
            identity_args.extend(["-c", f"user.name={name}"])
        if email:
            identity_args.extend(["-c", f"user.email={email}"])
        rc, out, err_msg = await run([*identity_args, "commit", "-m", message])
        return _format_result(rc, out, err_msg)

    elif action == "push":
        args = ["push"]
        if branch:
            err = _validate_ref(branch)
            if err:
                return f"Error: {err}"
            args.extend(["--set-upstream", "origin", branch])
        rc, out, err_msg = await run(args, timeout=60)
        return _format_result(rc, out, err_msg)

    elif action == "worktree_create":
        if not branch:
            return "Error: 'branch' is required for worktree_create"
        err = _validate_ref(branch)
        if err:
            return f"Error: {err}"

        # Keep the worktree inside the selected repository workspace so the
        # request-level path boundary also covers the new checkout.
        wt_base = os.path.join(repo_dir, ".agent-smith-worktrees")
        os.makedirs(wt_base, exist_ok=True)
        # Use branch name (sanitized) as directory name
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", branch)
        wt_path = os.path.join(wt_base, safe_name)

        if os.path.exists(wt_path):
            return f"Error: worktree path already exists: {wt_path}"

        rc, out, err_msg = await run(
            ["worktree", "add", wt_path, "-b", branch]
        )
        if rc == 0:
            return f"OK: worktree created at {wt_path} on branch {branch}"
        return _format_result(rc, out, err_msg)

    elif action == "worktree_remove":
        if not path:
            return "Error: 'path' is required for worktree_remove"

        if not os.path.isdir(path):
            return f"Error: worktree path does not exist: {path}"

        rc, out, err_msg = await run(
            ["worktree", "remove", path, "--force"]
        )
        return _format_result(rc, out, err_msg)

    elif action == "discover":
        sections: list[str] = []
        failures: list[str] = []

        async def probe(label: str, args: list[str]) -> str | None:
            """Run one discovery sub-command, recording rather than hiding failure.

            Dropping a section on a non-zero exit made a failed sub-command look
            exactly like an empty one — in a repo with no commits, ``git log``
            genuinely fails and its absence read as a healthy discovery.
            """
            rc, out, err = await run(args)
            if rc == 0:
                return out
            detail = (err or "").strip().splitlines()
            failures.append(f"{label}: {detail[0][:160] if detail else f'exit {rc}'}")
            return None

        current = await probe("current branch", ["branch", "--show-current"])
        if current is not None:
            sections.append(f"Current branch: {current.strip()}")

        branch_list = await probe("local branches", ["branch", "--format=%(refname:short)"])
        if branch_list and branch_list.strip():
            branches = branch_list.strip().splitlines()
            sections.append(f"Local branches ({len(branches)}): {', '.join(branches)}")

        remotes = await probe("remotes", ["remote", "-v"])
        if remotes and remotes.strip():
            sections.append(f"Remotes:\n{_redact_url_credentials(remotes.strip())}")

        history = await probe("recent commits", ["log", "--oneline", "-10", "--no-decorate"])
        if history and history.strip():
            sections.append(f"Recent commits:\n{history.strip()}")

        tree = await probe("working tree", ["status", "--short"])
        if tree is not None:
            if tree.strip():
                sections.append(f"Working tree ({len(tree.strip().splitlines())} changed files):\n{tree.strip()}")
            else:
                sections.append("Working tree: clean")

        if failures:
            sections.append(
                "Incomplete — these probes failed:\n"
                + "\n".join(f"- {failure}" for failure in failures)
            )
        return "\n\n".join(sections) if sections else "Error: could not discover repo info"

    else:
        return f"Error: unknown action '{action}'. Use: status, diff, branch_create, commit, push, worktree_create, worktree_remove, discover"
