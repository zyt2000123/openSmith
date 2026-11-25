"""Linux bubblewrap (bwrap) sandbox backend and explicit fail-closed / degraded adapters.

Implements the 7-stage mount order, namespace isolation, ``$HOME`` tmpfs
shielding, loopback-only ``lo`` bring-up under ``--unshare-net``, and Ubuntu
24.04+ AppArmor userns detection described in Direction 2.
"""

from __future__ import annotations

import asyncio
import atexit
import copy
import functools
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .config import CacheMount, ResolvedSandboxProfile
from .host import (
    CommandResult,
    LocalExecutionEnvironment,
    detect_network_denied_hint,
)
from .policy import (
    assert_no_control_characters,
    is_hardlink_protected_path,
    runtime_credential_paths,
)
from .preflight import HardlinkPreflightCache, check_hardlinks

_OPTIONAL_ENV_KEYS = ("LANG", "LC_ALL", "TERM", "TZ", "NO_COLOR")
_APPARMOR_BWRAP_PROFILE_REMEDIATION = """\
sudo tee /etc/apparmor.d/bwrap >/dev/null <<'EOF'
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
EOF
sudo systemctl reload apparmor"""

# Inline zero-dependency loopback bring-up snippet run inside bwrap's isolated
# userns + netns so `127.0.0.1` / `::1` work while outbound networking remains
# completely absent (no external NIC, empty routing table, /dev/null resolv.conf).
_LOOPBACK_UP_BOOTSTRAP = (
    "import fcntl, os, socket, struct, sys\n"
    "try:\n"
    "    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
    "    try:\n"
    "        ifr = struct.pack('16sH', b'lo', 0)\n"
    "        flags = struct.unpack('16sH', fcntl.ioctl(s.fileno(), 0x8913, ifr))[1]\n"
    "        fcntl.ioctl(s.fileno(), 0x8914, struct.pack('16sH', b'lo', flags | 0x1 | 0x40))\n"
    "    finally:\n"
    "        s.close()\n"
    "except OSError:\n"
    "    pass\n"
    "argv = sys.argv[1:]\n"
    "os.execvp(argv[0], argv)\n"
)

_SENTINEL_ROOT: Path | None = None
_SENTINEL_FILE: Path | None = None
_SENTINEL_DIR: Path | None = None


def _ensure_sentinels() -> tuple[Path, Path]:
    """Create mode-0000 file and dir sentinels used to blackhole sensitive paths."""
    global _SENTINEL_ROOT, _SENTINEL_FILE, _SENTINEL_DIR
    if (
        _SENTINEL_FILE is not None
        and _SENTINEL_DIR is not None
        and _SENTINEL_FILE.exists()
        and _SENTINEL_DIR.exists()
    ):
        return _SENTINEL_FILE, _SENTINEL_DIR
    root = Path(tempfile.mkdtemp(prefix="smith-bwrap-sentinels-"))
    os.chmod(root, 0o700)
    sfile = root / "denied-file"
    sdir = root / "denied-dir"
    sfile.write_bytes(b"")
    sdir.mkdir(mode=0o000)
    os.chmod(sfile, 0o000)
    os.chmod(sdir, 0o000)
    _SENTINEL_ROOT = root
    _SENTINEL_FILE = sfile
    _SENTINEL_DIR = sdir

    def _cleanup() -> None:
        try:
            if sfile.exists():
                os.chmod(sfile, 0o600)
            if sdir.exists():
                os.chmod(sdir, 0o700)
            shutil.rmtree(root, ignore_errors=True)
        except OSError:
            pass

    atexit.register(_cleanup)
    return sfile, sdir


def _read_sysctl(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _detect_distro_install_cmd() -> str:
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8").lower()
    except OSError:
        text = ""
    if "fedora" in text or "rhel" in text or "centos" in text or "rocky" in text:
        return "sudo dnf install -y bubblewrap"
    if "arch" in text or "manjaro" in text:
        return "sudo pacman -S --noconfirm bubblewrap"
    if "alpine" in text:
        return "sudo apk add bubblewrap"
    return "sudo apt update && sudo apt install -y bubblewrap"


def _is_container_env() -> bool:
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="ignore").lower()
        return any(token in cgroup for token in ("docker", "kubepods", "containerd", "lxc"))
    except OSError:
        return False


def _parse_bwrap_version(output: str) -> tuple[str | None, tuple[int, ...] | None]:
    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", output)
    if not match:
        return None, None
    ver_str = match.group(1)
    parts = tuple(int(p) for p in ver_str.split("."))
    return ver_str, parts


@dataclass(frozen=True)
class BwrapAvailability:
    ready: bool
    binary_path: str | None = None
    version: str | None = None
    code: str | None = None
    reason: str = ""
    remediation: str | None = None
    apparmor_restricted: bool = False
    userns_disabled: bool = False
    version_warning: str | None = None


@functools.lru_cache(maxsize=1)
def detect_bwrap_availability() -> BwrapAvailability:
    """Detect whether bubblewrap can create user/pid namespaces on this host."""
    return probe_bwrap_availability()


def _resolv_conf_bwrap_args(
    *,
    network: bool,
    resolv_conf_path: Path | None = None,
    resolv_is_symlink: bool | None = None,
    resolv_real_target: Path | None = None,
) -> list[str]:
    """Build bwrap mount args for ``/etc/resolv.conf`` after ``--tmpfs /run``.

    On Ubuntu/systemd-resolved, ``/etc/resolv.conf`` is a symlink into
    ``/run/systemd/resolve/stub-resolv.conf``.  Mounting ``/dev/null`` directly
    onto ``/etc/resolv.conf`` before ``/run`` exists fails in bwrap, while
    mounting ``--tmpfs /run`` without re-binding the symlink target breaks DNS
    when ``network=True``.  Handling regular files and symlinks explicitly
    keeps both modes working cleanly.
    """
    resolv_path = resolv_conf_path if resolv_conf_path is not None else Path("/etc/resolv.conf")
    if resolv_is_symlink is None:
        try:
            is_symlink = resolv_path.is_symlink()
        except OSError:
            is_symlink = False
    else:
        is_symlink = resolv_is_symlink

    real_target: Path | None = resolv_real_target
    if real_target is None and is_symlink:
        try:
            resolved = resolv_path.resolve(strict=False)
            real_target = resolved
        except OSError:
            real_target = None

    if not network:
        if not is_symlink:
            # Regular file (Debian/Alpine/Docker): mask directly when present.
            if resolv_conf_path is not None or resolv_path.exists():
                return ["--ro-bind", "/dev/null", "/etc/resolv.conf"]
            return []
        # Symlink (e.g. Ubuntu -> /run/systemd/resolve/stub-resolv.conf):
        # Because `--tmpfs /run` and `--tmpfs /tmp` are mounted before this,
        # binding `/dev/null` to a target under `/run` or `/tmp` succeeds and
        # makes `/etc/resolv.conf` resolve to an empty file.
        if real_target is not None:
            target_str = str(real_target)
            if target_str.startswith(("/run/", "/tmp/")):
                return ["--ro-bind", "/dev/null", target_str]
            if target_str.startswith("/etc/") and (
                resolv_real_target is not None or real_target.exists()
            ):
                return ["--ro-bind", "/dev/null", target_str]
        return []

    # network=True: if `/etc/resolv.conf` is a symlink into `/run/` or `/tmp/`,
    # re-bind the real host target read-only onto the empty tmpfs so DNS works
    # while the rest of host `/run` (docker.sock, dbus, etc.) stays hidden.
    if is_symlink and real_target is not None:
        target_str = str(real_target)
        if target_str.startswith(("/run/", "/tmp/")):
            if resolv_real_target is not None or real_target.is_file():
                return ["--ro-bind", target_str, target_str]
    return []


def probe_bwrap_availability(
    *,
    bwrap_path: str | None = None,
    unprivileged_userns_clone: str | None = None,
    apparmor_restrict_userns: str | None = None,
    in_container: bool | None = None,
    run_smoke=None,
    resolv_is_symlink: bool | None = None,
    resolv_real_target: Path | None = None,
) -> BwrapAvailability:
    """Uncached probe (with injectable seams for unit tests across platforms)."""
    binary = bwrap_path if bwrap_path is not None else shutil.which("bwrap")
    if not binary:
        return BwrapAvailability(
            ready=False,
            binary_path=None,
            code="BWRAP_NOT_INSTALLED",
            reason="bubblewrap (bwrap) is not installed or not in PATH",
            remediation=_detect_distro_install_cmd(),
        )

    version_str: str | None = None
    version_warning: str | None = None
    try:
        ver_proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        version_str, ver_tuple = _parse_bwrap_version(
            f"{ver_proc.stdout}\n{ver_proc.stderr}"
        )
        if ver_tuple is not None and ver_tuple < (0, 5, 0):
            version_warning = (
                f"bwrap {version_str} is older than 0.5.0; "
                "--unshare-cgroup-try may not be supported"
            )
    except (OSError, subprocess.SubprocessError):
        pass

    userns_val = (
        unprivileged_userns_clone
        if unprivileged_userns_clone is not None
        else _read_sysctl("/proc/sys/kernel/unprivileged_userns_clone")
    )
    apparmor_val = (
        apparmor_restrict_userns
        if apparmor_restrict_userns is not None
        else _read_sysctl("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
    )
    userns_disabled = userns_val == "0"
    apparmor_restricted = apparmor_val == "1"

    # Use the same Stage 1 namespace + mount arguments as real invocations
    # (including /etc, /proc, /dev, /tmp, /run, and resolv.conf handling) so
    # doctor catches mount/symlink/userns failures immediately.
    smoke_argv = [
        binary,
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-net",
        "--die-with-parent",
        "--new-session",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind-try", "/bin", "/bin",
        "--ro-bind-try", "/sbin", "/sbin",
        "--ro-bind-try", "/lib", "/lib",
        "--ro-bind-try", "/lib64", "/lib64",
        "--ro-bind-try", "/lib32", "/lib32",
        "--ro-bind", "/etc", "/etc",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
        *_resolv_conf_bwrap_args(
            network=False,
            resolv_is_symlink=resolv_is_symlink,
            resolv_real_target=resolv_real_target,
        ),
        "--clearenv",
        "--",
        "/bin/true",
    ]
    if run_smoke is not None:
        returncode, stderr = run_smoke(smoke_argv)
    else:
        try:
            proc = subprocess.run(
                smoke_argv,
                capture_output=True,
                text=True,
                timeout=10,
            )
            returncode, stderr = proc.returncode, (proc.stderr or "").strip()
        except (OSError, subprocess.SubprocessError) as exc:
            returncode, stderr = 1, str(exc)

    if returncode == 0:
        return BwrapAvailability(
            ready=True,
            binary_path=binary,
            version=version_str,
            apparmor_restricted=apparmor_restricted,
            userns_disabled=userns_disabled,
            version_warning=version_warning,
        )

    lowered = stderr.lower()
    if userns_disabled:
        return BwrapAvailability(
            ready=False,
            binary_path=binary,
            version=version_str,
            code="LINUX_USERNS_DISABLED",
            reason=f"Unprivileged user namespaces are disabled (kernel.unprivileged_userns_clone=0): {stderr}",
            remediation=(
                "sudo sysctl -w kernel.unprivileged_userns_clone=1\n"
                "echo 'kernel.unprivileged_userns_clone=1' | sudo tee /etc/sysctl.d/99-userns.conf"
            ),
            userns_disabled=True,
            apparmor_restricted=apparmor_restricted,
            version_warning=version_warning,
        )

    if apparmor_restricted and any(
        hint in lowered for hint in ("uid map", "operation not permitted", "permission denied")
    ):
        return BwrapAvailability(
            ready=False,
            binary_path=binary,
            version=version_str,
            code="LINUX_APPARMOR_USERNS_BLOCKED",
            reason=(
                "AppArmor restricts unprivileged user namespaces for bwrap "
                f"(kernel.apparmor_restrict_unprivileged_userns=1): {stderr}"
            ),
            remediation=_APPARMOR_BWRAP_PROFILE_REMEDIATION,
            apparmor_restricted=True,
            version_warning=version_warning,
        )

    container = in_container if in_container is not None else _is_container_env()
    if container:
        return BwrapAvailability(
            ready=False,
            binary_path=binary,
            version=version_str,
            code="LINUX_CONTAINER_USERNS_BLOCKED",
            reason=f"Container runtime blocked bubblewrap user namespaces: {stderr}",
            remediation=(
                "Run container with userns enabled (e.g. --security-opt seccomp=unconfined "
                "--security-opt apparmor=unconfined) or set sandbox.fallback_policy: "
                "host_with_approval in ~/.agent-smith/config.yaml if you accept host-level risk."
            ),
            apparmor_restricted=apparmor_restricted,
            version_warning=version_warning,
        )

    return BwrapAvailability(
        ready=False,
        binary_path=binary,
        version=version_str,
        code="BWRAP_SMOKE_FAILED",
        reason=f"bubblewrap smoke test failed (exit {returncode}): {stderr}",
        remediation="Run `smith doctor` to inspect kernel namespace settings.",
        apparmor_restricted=apparmor_restricted,
        version_warning=version_warning,
    )


def _find_sensitive_paths_under(
    roots: Iterable[Path],
    runtime_secret_paths: Iterable[Path],
) -> list[tuple[Path, bool]]:
    """Return ``(existing_path, is_dir)`` entries inside ``roots`` that must be blackholed."""
    found: dict[Path, bool] = {}
    root_list = tuple(roots)

    for secret in runtime_secret_paths:
        if not secret.exists():
            continue
        for base in root_list:
            try:
                if secret == base or secret.is_relative_to(base):
                    found[secret] = secret.is_dir()
                    break
            except ValueError:
                continue

    for base in root_list:
        if not base.is_dir():
            continue
        pending = [base]
        while pending:
            current = pending.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if stat.S_ISLNK(st.st_mode):
                            continue
                        entry_path = Path(entry.path)
                        try:
                            rel = entry_path.relative_to(base)
                        except ValueError:
                            continue
                        is_dir = stat.S_ISDIR(st.st_mode)
                        if is_hardlink_protected_path(rel):
                            # Blackhole at the highest sensitive directory/file;
                            # no need to descend further inside it.
                            found[entry_path] = is_dir
                            continue
                        if is_dir:
                            pending.append(entry_path)
            except OSError:
                continue

    return sorted(found.items(), key=lambda item: (len(item[0].parts), str(item[0])))


class LinuxBwrapEnvironment:
    """Linux sandbox backend powered by bubblewrap (``bwrap``)."""

    name = "sandbox"
    backend_id = "bwrap"

    def __init__(
        self,
        *,
        workspace: str | Path,
        runtime_secret_paths: Iterable[str | Path] | None = None,
        non_delegable_write_paths: Iterable[str | Path] = (),
        approved_host_access: bool = False,
        profile: ResolvedSandboxProfile | None = None,
        bwrap_binary: str | None = None,
        host_home: Path | None = None,
    ) -> None:
        if profile is not None:
            self._workspace = profile.workspace
            self._extra_roots: tuple[Path, ...] = profile.extra_roots
            self._share_read_only: tuple[Path, ...] = profile.share_read_only
            self._cache_mounts: tuple[CacheMount, ...] = profile.cache_mounts
            self._path_entries: tuple[Path, ...] = profile.path_entries
            self._default_network = profile.default_network
            configured_secrets = (
                profile.runtime_secret_paths
                if runtime_secret_paths is None
                else tuple(Path(p).expanduser().resolve() for p in runtime_secret_paths)
            )
            configured_non_delegable = (
                tuple(non_delegable_write_paths)
                if non_delegable_write_paths
                else profile.non_delegable_write_paths
            )
        else:
            self._workspace = Path(workspace).expanduser().resolve()
            self._extra_roots = ()
            self._share_read_only = ()
            self._cache_mounts = ()
            self._path_entries = ()
            self._default_network = "loopback_only"
            configured_secrets = (
                runtime_credential_paths()
                if runtime_secret_paths is None
                else tuple(Path(p).expanduser().resolve() for p in runtime_secret_paths)
            )
            configured_non_delegable = tuple(non_delegable_write_paths)

        assert_no_control_characters(self._workspace, label="sandbox workspace path")
        if is_hardlink_protected_path(self._workspace):
            raise ValueError(
                f"sandbox workspace is inside a protected directory: {self._workspace}"
            )
        self._runtime_secret_paths = tuple(configured_secrets)
        for p in self._runtime_secret_paths:
            assert_no_control_characters(p, label="runtime secret path")
        self._non_delegable_write_paths = tuple(
            Path(p).expanduser().resolve() for p in configured_non_delegable
        )
        for p in self._non_delegable_write_paths:
            assert_no_control_characters(p, label="non-delegable write path")

        self._bwrap_binary = bwrap_binary
        self._host_home = (host_home if host_home is not None else Path.home()).expanduser().resolve()
        self._approved_host_access = approved_host_access
        self._approved_network = approved_host_access
        self._host = LocalExecutionEnvironment()
        self._preflight_cache = HardlinkPreflightCache()
        self.last_result: CommandResult | None = None

    @property
    def _rw_workspace_roots(self) -> tuple[Path, ...]:
        return (self._workspace, *self._extra_roots)

    @property
    def _all_writable_roots(self) -> tuple[Path, ...]:
        return (
            *self._rw_workspace_roots,
            *(mount.host_path for mount in self._cache_mounts),
        )

    def with_non_delegable_write_paths(
        self,
        paths: Iterable[str | Path],
    ) -> LinuxBwrapEnvironment:
        scoped = copy.copy(self)
        resolved = tuple(Path(p).expanduser().resolve() for p in paths)
        for p in resolved:
            assert_no_control_characters(p, label="non-delegable write path")
        scoped._non_delegable_write_paths = resolved
        scoped._preflight_cache = HardlinkPreflightCache()
        return scoped

    def with_approval_scope(self, scope: object) -> LinuxBwrapEnvironment:
        scoped = copy.copy(self)
        if bool(getattr(scope, "grants_host_execution", False)):
            scoped._approved_host_access = True
            scoped._approved_network = True
        elif bool(getattr(scope, "grants_network_execution", False)):
            scoped._approved_host_access = False
            scoped._approved_network = True
        else:
            scoped._approved_host_access = False
            scoped._approved_network = False
        return scoped

    def _validate_cwd(self, cwd: str | None) -> tuple[str | None, str | None]:
        resolved = Path(cwd).expanduser().resolve() if cwd else self._workspace
        if self._approved_host_access:
            return str(resolved), None
        for root in self._rw_workspace_roots:
            try:
                resolved.relative_to(root)
                return str(resolved), None
            except ValueError:
                continue
        return None, f"sandbox working directory escapes workspace: {resolved}"

    def _safe_environment(self, requested: dict[str, str] | None) -> dict[str, str]:
        path_value = (
            os.pathsep.join(str(p) for p in self._path_entries)
            if self._path_entries
            else os.defpath
        )
        environment = {
            "PATH": path_value,
            "HOME": str(self._workspace),
            "TMPDIR": "/tmp",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "PIP_CONFIG_FILE": os.devnull,
            "NPM_CONFIG_USERCONFIG": os.devnull,
        }
        for mount in self._cache_mounts:
            for key, value in mount.env_vars:
                environment[key] = value
        if requested:
            for key in _OPTIONAL_ENV_KEYS:
                value = requested.get(key)
                if value:
                    environment[key] = value
        return environment

    def _build_bwrap_argv(
        self,
        *,
        bwrap_bin: str,
        resolved_cwd: str,
        inner_argv: list[str],
        env: dict[str, str],
        network: bool,
        sentinel_file: Path,
        sentinel_dir: Path,
        resolv_is_symlink: bool | None = None,
        resolv_real_target: Path | None = None,
        sensitive_paths: list[tuple[Path, bool]] | None = None,
    ) -> list[str]:
        """Construct the exact 7-stage bubblewrap invocation."""
        args: list[str] = [
            bwrap_bin,
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup-try",
            "--die-with-parent",
            "--new-session",
        ]
        if not network:
            args.append("--unshare-net")
            if self._default_network == "none":
                args.extend(["--cap-drop", "ALL"])
        else:
            args.extend(["--cap-drop", "ALL"])

        # Stage 1: Base read-only OS skeleton & isolated /proc, /dev, /tmp, /run.
        # Mount --tmpfs /run BEFORE handling /etc/resolv.conf so Ubuntu's
        # /etc/resolv.conf -> /run/systemd/resolve/stub-resolv.conf symlink
        # resolves cleanly in both offline and online modes.
        args.extend([
            "--ro-bind", "/usr", "/usr",
            "--ro-bind-try", "/bin", "/bin",
            "--ro-bind-try", "/sbin", "/sbin",
            "--ro-bind-try", "/lib", "/lib",
            "--ro-bind-try", "/lib64", "/lib64",
            "--ro-bind-try", "/lib32", "/lib32",
            "--ro-bind", "/etc", "/etc",
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--tmpfs", "/run",
        ])
        args.extend(
            _resolv_conf_bwrap_args(
                network=network,
                resolv_is_symlink=resolv_is_symlink,
                resolv_real_target=resolv_real_target,
            )
        )

        # Stage 2: Shield host $HOME completely with empty tmpfs
        args.extend(["--tmpfs", str(self._host_home)])

        # Stage 3: Controlled read-only shares (toolchains, runtimes, skills)
        for ro_path in self._share_read_only:
            args.extend(["--ro-bind", str(ro_path), str(ro_path)])

        # Stage 4: Controlled read-write package caches
        for mount in self._cache_mounts:
            args.extend(["--bind", str(mount.host_path), str(mount.host_path)])

        # Stage 5: Workspace + extra_roots (RW)
        for rw_root in self._rw_workspace_roots:
            args.extend(["--bind", str(rw_root), str(rw_root)])

        # Stage 6: Blackhole existing sensitive files/dirs inside workspace,
        # extra_roots, AND share_read_only trees (M4).
        blackhole_targets = (
            sensitive_paths
            if sensitive_paths is not None
            else _find_sensitive_paths_under(
                (*self._rw_workspace_roots, *self._share_read_only),
                self._runtime_secret_paths,
            )
        )
        for target_path, is_dir in blackhole_targets:
            sentinel = sentinel_dir if is_dir else sentinel_file
            args.extend(["--ro-bind", str(sentinel), str(target_path)])

        # Stage 7: Lock non-delegable write paths read-only last (highest precedence)
        for protected in self._non_delegable_write_paths:
            if protected.exists():
                args.extend(["--ro-bind", str(protected), str(protected)])

        # Scrub environment and inject explicit safe vars
        args.append("--clearenv")
        for key, value in sorted(env.items()):
            args.extend(["--setenv", key, value])

        args.extend(["--chdir", resolved_cwd, "--"])

        if not network and self._default_network == "loopback_only":
            args.extend([sys.executable, "-c", _LOOPBACK_UP_BOOTSTRAP, *inner_argv])
        else:
            args.extend(inner_argv)
        return args

    def _sensitive_hardlink_error(self) -> str | None:
        return check_hardlinks(
            self._rw_workspace_roots,
            self._non_delegable_write_paths,
            cache_roots=tuple(m.host_path for m in self._cache_mounts),
            runtime_secret_paths=self._runtime_secret_paths,
        )

    async def run_command(
        self,
        command: str | None = None,
        *,
        argv: list[str] | None = None,
        cwd: str | None = None,
        timeout_seconds: float = 30.0,
        env: dict[str, str] | None = None,
        network: bool = False,
    ) -> CommandResult:
        if sys.platform != "linux":
            return CommandResult(
                exit_code=None,
                error="Linux bubblewrap sandbox is unavailable on this platform",
                sandbox_backend="bwrap",
            )
        if (command is None) == (argv is None):
            return CommandResult(
                exit_code=None,
                error="exactly one of command or argv is required",
                sandbox_backend="bwrap",
            )
        if not isinstance(network, bool):
            return CommandResult(
                exit_code=None,
                error="network parameter must be a boolean",
                sandbox_backend="bwrap",
            )
        if network and not self._approved_network:
            return CommandResult(
                exit_code=None,
                error="outbound network access requires an approved sandbox_network scope",
                sandbox_backend="bwrap",
            )

        bwrap_bin = self._bwrap_binary or shutil.which("bwrap")
        if not bwrap_bin:
            return CommandResult(
                exit_code=None,
                error="[BWRAP_NOT_INSTALLED] bubblewrap (bwrap) is unavailable. Run `smith doctor` for fix instructions.",
                sandbox_backend="bwrap",
            )

        resolved_cwd, error = self._validate_cwd(cwd)
        if error:
            return CommandResult(
                exit_code=None,
                error=error,
                sandbox_backend="bwrap",
            )
        assert resolved_cwd is not None

        hardlink_error = await asyncio.to_thread(
            self._preflight_cache.check,
            self._all_writable_roots,
            self._non_delegable_write_paths,
            walk_fn=self._sensitive_hardlink_error,
        )
        if hardlink_error:
            return CommandResult(
                exit_code=None,
                error=hardlink_error,
                sandbox_backend="bwrap",
            )

        sentinel_file, sentinel_dir = _ensure_sentinels()
        inner_argv = ["/bin/sh", "-c", command] if command is not None else list(argv or [])
        effective_network = bool(network or self._approved_host_access)
        safe_env = self._safe_environment(env)
        bwrap_argv = self._build_bwrap_argv(
            bwrap_bin=bwrap_bin,
            resolved_cwd=resolved_cwd,
            inner_argv=inner_argv,
            env=safe_env,
            network=effective_network,
            sentinel_file=sentinel_file,
            sentinel_dir=sentinel_dir,
        )
        result = await self._host.run_command(
            argv=bwrap_argv,
            cwd=resolved_cwd,
            timeout_seconds=timeout_seconds,
            env=safe_env,
        )
        result.sandbox_backend = "bwrap"
        result.network_used = effective_network
        if not effective_network and result.exit_code not in (None, 0):
            result.network_denied_hint = detect_network_denied_hint(
                result.stdout, result.stderr
            )
        self.last_result = result
        return result


class UnavailableSandboxEnvironment:
    """Fail-closed sandbox placeholder when no OS sandbox backend is ready."""

    name = "sandbox"
    backend_id = "unavailable"
    is_available = False
    is_degraded = False

    def __init__(
        self,
        *,
        reason: str,
        code: str = "SANDBOX_UNAVAILABLE",
        remediation: str = "Run `smith doctor` for fix instructions.",
    ) -> None:
        self.reason = reason
        self.code = code
        self.remediation = remediation

    def with_non_delegable_write_paths(
        self, paths: Iterable[str | Path]
    ) -> UnavailableSandboxEnvironment:
        return self

    def with_approval_scope(self, scope: object) -> UnavailableSandboxEnvironment:
        return self

    async def run_command(
        self,
        command: str | None = None,
        *,
        argv: list[str] | None = None,
        cwd: str | None = None,
        timeout_seconds: float = 30.0,
        env: dict[str, str] | None = None,
        network: bool = False,
    ) -> CommandResult:
        return CommandResult(
            exit_code=None,
            error=f"[{self.code}] {self.reason}. {self.remediation}",
            sandbox_backend="unavailable",
        )


class DegradedHostSandboxEnvironment:
    """Explicit opt-in fallback (``fallback_policy: host_with_approval``).

    Provides credential-scrubbed host execution only when explicitly opted in;
    ``ToolGuard`` elevates every shell call under this backend to CRITICAL.
    """

    name = "sandbox"
    backend_id = "host_degraded"
    is_available = True
    is_degraded = True

    def __init__(
        self,
        *,
        workspace: str | Path,
        reason: str = "OS sandbox unavailable; running in explicit host_with_approval fallback mode",
        non_delegable_write_paths: Iterable[str | Path] = (),
        profile: ResolvedSandboxProfile | None = None,
    ) -> None:
        self._workspace = (
            profile.workspace
            if profile is not None
            else Path(workspace).expanduser().resolve()
        )
        self._path_entries = profile.path_entries if profile is not None else ()
        self._cache_mounts = profile.cache_mounts if profile is not None else ()
        self.reason = reason
        self._non_delegable_write_paths = tuple(
            Path(p).expanduser().resolve() for p in non_delegable_write_paths
        )
        self._host = LocalExecutionEnvironment()
        self.last_result: CommandResult | None = None

    def with_non_delegable_write_paths(
        self, paths: Iterable[str | Path]
    ) -> DegradedHostSandboxEnvironment:
        scoped = copy.copy(self)
        scoped._non_delegable_write_paths = tuple(
            Path(p).expanduser().resolve() for p in paths
        )
        return scoped

    def with_approval_scope(self, scope: object) -> DegradedHostSandboxEnvironment:
        return self

    def _safe_environment(self, requested: dict[str, str] | None) -> dict[str, str]:
        path_value = (
            os.pathsep.join(str(p) for p in self._path_entries)
            if self._path_entries
            else (os.environ.get("PATH") or os.defpath)
        )
        environment = {
            "PATH": path_value,
            "HOME": str(self._workspace),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "PIP_CONFIG_FILE": os.devnull,
            "NPM_CONFIG_USERCONFIG": os.devnull,
        }
        for mount in self._cache_mounts:
            for key, value in mount.env_vars:
                environment[key] = value
        if requested:
            for key in _OPTIONAL_ENV_KEYS:
                value = requested.get(key)
                if value:
                    environment[key] = value
        return environment

    async def run_command(
        self,
        command: str | None = None,
        *,
        argv: list[str] | None = None,
        cwd: str | None = None,
        timeout_seconds: float = 30.0,
        env: dict[str, str] | None = None,
        network: bool = False,
    ) -> CommandResult:
        result = await self._host.run_command(
            command=command,
            argv=argv,
            cwd=cwd or str(self._workspace),
            timeout_seconds=timeout_seconds,
            env=self._safe_environment(env),
            network=network,
        )
        result.sandbox_backend = "host_degraded"
        result.network_used = True
        self.last_result = result
        return result


__all__ = (
    "BwrapAvailability",
    "DegradedHostSandboxEnvironment",
    "LinuxBwrapEnvironment",
    "UnavailableSandboxEnvironment",
    "detect_bwrap_availability",
    "probe_bwrap_availability",
)
