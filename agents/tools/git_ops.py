from __future__ import annotations

"""Git workflow tool provider — branch, commit, push, worktree operations.

Runs every git command through the injected execution environment in argv
mode (no shell interpretation). Validates inputs to prevent injection and
checks for sensitive files before staging.
"""

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
    secrets owned by the Agent-Smith runtime.
    """
    home = os.path.abspath(cwd) if cwd else os.getcwd()
    environment = {
        "PATH": os.environ.get("PATH") or os.defpath,
        "HOME": home,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
    }
    for key in _SAFE_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment


async def _run_git(
    args: list[str], cwd: str | None = None, timeout: int = 30, environment=None
) -> tuple[int, str, str]:
    """Run a git command via the execution environment; return (returncode, stdout, stderr)."""
    if environment is None:
        return -1, "", "no execution environment is available for git"
    result = await environment.run_command(
        argv=["git", *args],
        cwd=cwd,
        timeout_seconds=timeout,
        env=_safe_environment(cwd),
    )
    if result.timed_out:
        return -1, "", f"git command timed out after {timeout}s"
    if result.error:
        return -1, "", result.error
    exit_code = result.exit_code if result.exit_code is not None else -1
    return exit_code, result.stdout, result.stderr


def _format_result(returncode: int, stdout: str, stderr: str) -> str:
    """Format git output into a single result string."""
    parts: list[str] = []
    if stdout:
        text = stdout if len(stdout) <= MAX_OUTPUT else stdout[:MAX_OUTPUT] + "\n... (truncated)"
        parts.append(text)
    if stderr:
        text = stderr if len(stderr) <= MAX_OUTPUT else stderr[:MAX_OUTPUT] + "\n... (truncated)"
        parts.append(f"[stderr]\n{text}")
    body = "\n".join(parts) if parts else "(no output)"
    return f"[exit_code={returncode}]\n{body}"


def _check_sensitive_files(files: list[str]) -> list[str]:
    """Return list of sensitive file paths that should not be staged."""
    return [f for f in files if _SENSITIVE_PATTERNS.search(f)]


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

        # Determine files to stage
        if files:
            # Check for sensitive files in explicit list
            sensitive = _check_sensitive_files(files)
            if sensitive:
                return (
                    f"Error: refusing to stage sensitive files: {', '.join(sensitive)}. "
                    f"Remove them from the files list or add them to .gitignore."
                )
            rc, out, err_msg = await run(["add", "--"] + files)
            if rc != 0:
                return _format_result(rc, out, err_msg)
        else:
            # Stage all tracked changes, but check for sensitive files first
            rc_diff, diff_out, _ = await run(
                ["diff", "--name-only", "--diff-filter=ACMR"]
            )
            rc_untracked, untracked_out, _ = await run(
                ["ls-files", "--others", "--exclude-standard"]
            )
            all_files = []
            if diff_out.strip():
                all_files.extend(diff_out.strip().splitlines())
            if untracked_out.strip():
                all_files.extend(untracked_out.strip().splitlines())

            sensitive = _check_sensitive_files(all_files)
            if sensitive:
                return (
                    f"Error: refusing to stage sensitive files: {', '.join(sensitive)}. "
                    f"Add them to .gitignore or specify files explicitly."
                )
            # Stage tracked modifications
            rc, out, err_msg = await run(["add", "-u"])
            if rc != 0:
                return _format_result(rc, out, err_msg)

        # Commit
        rc, out, err_msg = await run(["commit", "-m", message])
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

        # Current branch
        rc, out, _ = await run(["branch", "--show-current"])
        if rc == 0:
            sections.append(f"Current branch: {out.strip()}")

        # All local branches
        rc, out, _ = await run(["branch", "--format=%(refname:short)"])
        if rc == 0 and out.strip():
            branches = out.strip().splitlines()
            sections.append(f"Local branches ({len(branches)}): {', '.join(branches)}")

        # Remotes
        rc, out, _ = await run(["remote", "-v"])
        if rc == 0 and out.strip():
            sections.append(f"Remotes:\n{out.strip()}")

        # Recent history (last 10 commits, oneline)
        rc, out, _ = await run(
            ["log", "--oneline", "-10", "--no-decorate"]
        )
        if rc == 0 and out.strip():
            sections.append(f"Recent commits:\n{out.strip()}")

        # Dirty state
        rc, out, _ = await run(["status", "--short"])
        if rc == 0:
            if out.strip():
                sections.append(f"Working tree ({len(out.strip().splitlines())} changed files):\n{out.strip()}")
            else:
                sections.append("Working tree: clean")

        return "\n\n".join(sections) if sections else "Error: could not discover repo info"

    else:
        return f"Error: unknown action '{action}'. Use: status, diff, branch_create, commit, push, worktree_create, worktree_remove, discover"
