"""macOS Seatbelt adapter for sandboxed tool execution."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from itertools import pairwise
from pathlib import Path

from .host import CommandResult, LocalExecutionEnvironment

_SANDBOX_EXECUTABLE = "/usr/bin/sandbox-exec"
_OPTIONAL_ENV_KEYS = ("LANG", "LC_ALL", "TERM", "TZ", "NO_COLOR")
_CREDENTIAL_DIRECTORIES = frozenset(
    {".ssh", ".gnupg", ".aws", ".kube", ".agent-smith", ".docker"}
)
_CREDENTIAL_CONFIGS = frozenset({".npmrc", ".pypirc", ".netrc", ".git-credentials"})
_PRIVATE_KEY_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx"})
_PRIVATE_KEY_NAMES = frozenset({"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"})


def _is_sensitive_data_path(path: Path) -> bool:
    """Whether shell access to this workspace-relative path is always denied."""
    lowered_parts = tuple(part.lower() for part in path.parts)
    name = path.name.lower()
    pairs = tuple(pairwise(lowered_parts))
    return (
        any(part in _CREDENTIAL_DIRECTORIES for part in lowered_parts)
        or any(
            parent == ".config" and child in {"gh", "gcloud"} for parent, child in pairs
        )
        or ("library", "keychains") in pairs
        or name == ".env"
        or name.startswith(".env.")
        or name in _CREDENTIAL_CONFIGS
        or name in _PRIVATE_KEY_NAMES
        or path.suffix.lower() in _PRIVATE_KEY_SUFFIXES
    )


def _is_hardlink_protected_path(path: Path) -> bool:
    lowered_parts = tuple(part.lower() for part in path.parts)
    return _is_sensitive_data_path(path) or ".git" in lowered_parts


class MacOSSeatbeltEnvironment:
    """Run commands under a deny-by-default Seatbelt profile.

    The workspace is the only writable location, with credential data and Git
    metadata protected even inside it. Network access is explicitly denied.
    A preflight rejects hard-link aliases that a path-based profile cannot
    distinguish. This backend fails closed on a non-macOS host or when
    ``sandbox-exec`` is unavailable.
    """

    name = "sandbox"

    def __init__(self, *, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser().resolve()
        if any(
            ord(character) < 32 or ord(character) == 127
            for character in str(self._workspace)
        ):
            raise ValueError("sandbox workspace path contains control characters")
        if _is_hardlink_protected_path(self._workspace):
            raise ValueError(
                f"sandbox workspace is inside a protected directory: {self._workspace}"
            )
        self._host = LocalExecutionEnvironment()

    def _profile(self) -> str:
        # ``system.sb`` supplies the narrow macOS runtime grants needed by
        # dynamically linked command-line programs.  The enclosing default
        # deny still blocks arbitrary files, writes, and networking; the
        # workspace grants below are the only project-data exception.
        #
        # WORKSPACE is passed with ``sandbox-exec -D`` rather than interpolated
        # into this source.  ``regex-quote`` keeps every user-controlled path
        # character out of the SBPL grammar and regex language.
        return r"""(version 1)
(deny default)
(import "system.sb")
(deny network*)
(allow process*)
(allow file-read* (literal "/private/var/select/sh"))
(allow file-read* (subpath "/Applications/Xcode.app"))
(allow file-read* (subpath "/Library/Developer"))
(allow file-read* (literal "/private/var/db/xcode_select_link"))
(allow file-read-metadata (path-ancestors (param "WORKSPACE")))
(allow file-read* (subpath (param "WORKSPACE")))
(allow file-write* (subpath (param "WORKSPACE")))
(deny file-read* file-write*
    (regex (string-append "^" (regex-quote (param "WORKSPACE"))
        #"/(.*/)?\.env($|\..*)")))
(deny file-read* file-write*
    (regex (string-append "^" (regex-quote (param "WORKSPACE"))
        #"/(.*/)?\.(ssh|gnupg|aws|kube|agent-smith|docker)(/|$)")))
(deny file-read* file-write*
    (regex (string-append "^" (regex-quote (param "WORKSPACE"))
        #"/(.*/)?\.config/(gh|gcloud)(/|$)")))
(deny file-read* file-write*
    (regex (string-append "^" (regex-quote (param "WORKSPACE"))
        #"/(.*/)?Library/Keychains(/|$)")))
(deny file-read* file-write*
    (regex (string-append "^" (regex-quote (param "WORKSPACE"))
        #"/.*\.(pem|key|p12|pfx)$")))
(deny file-read* file-write*
    (regex (string-append "^" (regex-quote (param "WORKSPACE"))
        #"/(.*/)?id_(rsa|dsa|ecdsa|ed25519)$")))
(deny file-write*
    (regex (string-append "^" (regex-quote (param "WORKSPACE"))
        #"/(.*/)?\.git(/|$)")))
(deny file-read* file-write*
    (regex (string-append "^" (regex-quote (param "WORKSPACE"))
        #"/(.*/)?\.(npmrc|pypirc|netrc|git-credentials)$")))
"""

    def _validate_cwd(self, cwd: str | None) -> tuple[str | None, str | None]:
        resolved = Path(cwd).expanduser().resolve() if cwd else self._workspace
        try:
            resolved.relative_to(self._workspace)
        except ValueError:
            return None, f"sandbox working directory escapes workspace: {resolved}"
        return str(resolved), None

    def _safe_environment(self, requested: dict[str, str] | None) -> dict[str, str]:
        """Build the complete child environment; never inherit server secrets."""
        environment = {
            "PATH": os.defpath,
            "HOME": str(self._workspace),
            "TMPDIR": str(self._workspace),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "PIP_CONFIG_FILE": os.devnull,
            "NPM_CONFIG_USERCONFIG": os.devnull,
        }
        if requested:
            for key in _OPTIONAL_ENV_KEYS:
                value = requested.get(key)
                if value:
                    environment[key] = value
        return environment

    def _sensitive_hardlink_error(self) -> str | None:
        """Reject path aliases that a path-based Seatbelt profile cannot identify.

        Two distinct aliases matter, and only these two:

        * A link inside the workspace whose inode is also reachable at a
          protected path (``.env``, ``.git/…``).  The profile denies the
          protected *path*, but the alias reaches the same *inode*.
        * A link whose inode has more references than the workspace accounts
          for.  The extra reference is outside, and nothing here can prove what
          it points at, so it fails closed.

        A file with several plain links entirely inside the workspace is left
        alone — that is what package stores and build caches produce, and
        refusing it would disable shell for the whole workspace.
        """
        walk_errors: list[OSError] = []
        # inode -> (paths seen inside the workspace, declared link count)
        seen: dict[int, tuple[list[Path], int]] = {}

        for root, directories, files in os.walk(
            self._workspace,
            followlinks=False,
            onerror=walk_errors.append,
        ):
            root_path = Path(root)
            directories[:] = [
                name for name in directories if not (root_path / name).is_symlink()
            ]
            for name in files:
                path = root_path / name
                try:
                    relative = path.relative_to(self._workspace)
                    metadata = path.lstat()
                except (OSError, ValueError):
                    return (
                        "sandbox protected-path preflight could not inspect workspace"
                    )
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink <= 1
                ):
                    continue
                paths, _ = seen.setdefault(metadata.st_ino, ([], metadata.st_nlink))
                paths.append(relative)

        if walk_errors:
            return "sandbox protected-path preflight could not inspect workspace"

        for paths, link_count in seen.values():
            aliases = ", ".join(str(path) for path in sorted(paths, key=str))
            protected = next(
                (path for path in paths if _is_hardlink_protected_path(path)), None
            )
            if protected is not None:
                return (
                    "sandbox refuses files with hard links to a protected path "
                    f"({protected}): {aliases}"
                )
            if link_count > len(paths):
                return (
                    "sandbox refuses files with hard links reaching outside the "
                    f"workspace: {aliases} (copy the file, or re-create it "
                    "without hard links, to run shell here)"
                )
        return None

    async def run_command(
        self,
        command: str | None = None,
        *,
        argv: list[str] | None = None,
        cwd: str | None = None,
        timeout_seconds: float = 30.0,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        if sys.platform != "darwin":
            return CommandResult(
                exit_code=None, error="macOS Seatbelt is unavailable on this platform"
            )
        if not os.path.isfile(_SANDBOX_EXECUTABLE) or not os.access(
            _SANDBOX_EXECUTABLE, os.X_OK
        ):
            return CommandResult(
                exit_code=None, error="macOS sandbox-exec is unavailable"
            )
        if (command is None) == (argv is None):
            return CommandResult(
                exit_code=None, error="exactly one of command or argv is required"
            )
        resolved_cwd, error = self._validate_cwd(cwd)
        if error:
            return CommandResult(exit_code=None, error=error)
        # The preflight walks the whole workspace; on a large repository that
        # is hundreds of milliseconds of stat calls, which must not block the
        # event loop and stall every other request in the process.
        hardlink_error = await asyncio.to_thread(self._sensitive_hardlink_error)
        if hardlink_error:
            return CommandResult(exit_code=None, error=hardlink_error)

        wrapped_argv = [
            _SANDBOX_EXECUTABLE,
            "-D",
            f"WORKSPACE={self._workspace}",
            "-p",
            self._profile(),
        ]
        if command is not None:
            wrapped_argv.extend(["/bin/sh", "-lc", command])
        else:
            assert argv is not None
            wrapped_argv.extend(argv)
        return await self._host.run_command(
            argv=wrapped_argv,
            cwd=resolved_cwd,
            timeout_seconds=timeout_seconds,
            env=self._safe_environment(env),
        )
