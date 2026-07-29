"""macOS Seatbelt adapter for sandboxed tool execution."""

from __future__ import annotations

import asyncio
import copy
import os
import stat
import sys
from collections.abc import Iterable
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
_DEFAULT_RUNTIME_SECRET_PATHS = (
    Path.home() / ".agent-smith" / "config.yaml",
    Path.home() / ".agent-smith" / "config.yml",
    Path.home() / ".agent-smith" / "agent" / "config.yaml",
    Path.home() / ".agent-smith" / "agent" / "config.yml",
)


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

    By default the workspace is the only writable location, credential data
    and Git metadata remain protected, and network access is denied. A matching
    approved host-command capability creates a copy with temporary host access
    while still denying Agent-Smith runtime credential paths and inherited
    service credentials. A preflight rejects hard-link aliases that a
    path-based profile cannot distinguish. This backend fails closed on a
    non-macOS host or when ``sandbox-exec`` is unavailable.
    """

    name = "sandbox"

    def __init__(
        self,
        *,
        workspace: str | Path,
        runtime_secret_paths: Iterable[str | Path] | None = None,
        approved_host_access: bool = False,
    ) -> None:
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
        configured_secrets = (
            _DEFAULT_RUNTIME_SECRET_PATHS
            if runtime_secret_paths is None
            else tuple(runtime_secret_paths)
        )
        self._runtime_secret_paths = tuple(
            Path(path).expanduser().resolve() for path in configured_secrets
        )
        if any(
            ord(character) < 32 or ord(character) == 127
            for path in self._runtime_secret_paths
            for character in str(path)
        ):
            raise ValueError("runtime secret path contains control characters")
        self._approved_host_access = approved_host_access
        self._host = LocalExecutionEnvironment()

    def with_approval_scope(self, scope: object) -> MacOSSeatbeltEnvironment:
        """Return a one-call environment matching an exact approved host scope.

        ToolRegistry calls this only after binding *scope* to a normalized call
        and a broker-issued approval id. This method turns that verified
        capability into an OS profile without mutating the default sandbox.
        """
        if not bool(getattr(scope, "grants_host_execution", False)):
            return self
        # Preserve subclasses used by alternate backends and contract tests.
        # Reconstructing ``MacOSSeatbeltEnvironment`` directly would silently
        # discard their instrumentation or additional execution controls.
        scoped = copy.copy(self)
        scoped._approved_host_access = True
        return scoped

    def _profile(self) -> str:
        if self._approved_host_access:
            runtime_denies = "\n".join(
                "(deny file-read* file-write* "
                f"(literal (param \"RUNTIME_SECRET_{index}\")))"
                for index, _ in enumerate(self._runtime_secret_paths)
            )
            return f"""(version 1)
(deny default)
(import "system.sb")
(allow process*)
(allow network*)
(allow file-read* file-write*)
{runtime_denies}
"""
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
(allow file-read-metadata (subpath "/bin"))
(allow file-read-metadata (subpath "/usr/bin"))
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
        if self._approved_host_access:
            return str(resolved), None
        try:
            resolved.relative_to(self._workspace)
        except ValueError:
            return None, f"sandbox working directory escapes workspace: {resolved}"
        return str(resolved), None

    def _safe_environment(self, requested: dict[str, str] | None) -> dict[str, str]:
        """Build the complete child environment; never inherit server secrets."""
        environment = {
            "PATH": os.defpath,
            "HOME": str(Path.home() if self._approved_host_access else self._workspace),
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
        for index, runtime_secret_path in enumerate(self._runtime_secret_paths):
            wrapped_argv[1:1] = ["-D", f"RUNTIME_SECRET_{index}={runtime_secret_path}"]
        if command is not None:
            # The execution environment is already constructed explicitly in
            # ``_safe_environment``.  A login shell tries to load host startup
            # files such as /etc/profile, which Seatbelt correctly denies and
            # can also replace the minimal PATH before the command runs.
            wrapped_argv.extend(["/bin/sh", "-c", command])
        else:
            assert argv is not None
            wrapped_argv.extend(argv)
        return await self._host.run_command(
            argv=wrapped_argv,
            cwd=resolved_cwd,
            timeout_seconds=timeout_seconds,
            env=self._safe_environment(env),
        )
