"""Offline-capable environment and live sandbox containment diagnostics (``smith doctor``).

Deliberately imports nothing from ``engine.safety`` or ``server`` to preserve
the repository's four-layer dependency boundaries.  Audit-chain verification
goes directly through ``common.hash_chain.verify_chain``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from common.hash_chain import verify_chain
from common.yaml_utils import YamlConfigError, load_yaml

from .config import ResolvedSandboxProfile, SandboxProfileConfig, resolve_sandbox_profile
from .factory import create_sandbox_environment
from .linux_bwrap import BwrapAvailability, detect_bwrap_availability
from .macos_seatbelt import _SANDBOX_EXECUTABLE

CheckStatus = Literal["pass", "warn", "fail", "skip"]
_AUDIT_CHAIN_NAMESPACE = "agent-smith-audit"


@dataclass(frozen=True)
class DoctorCheck:
    id: str
    category: str
    status: CheckStatus
    summary: str
    detail: str = ""
    code: str | None = None
    remediation: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "category": self.category,
            "status": self.status,
            "summary": self.summary,
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.code:
            payload["code"] = self.code
        if self.remediation:
            payload["remediation"] = self.remediation
        return payload


@dataclass(frozen=True)
class SandboxHealthReport:
    platform: str
    backend: str
    ready: bool
    binary_path: str | None
    binary_version: str | None
    checks: tuple[DoctorCheck, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "backend": self.backend,
            "ready": self.ready,
            "binary_path": self.binary_path,
            "binary_version": self.binary_version,
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass(frozen=True)
class DoctorReport:
    ok: bool
    platform: str
    cwd: str
    sandbox_backend: str
    fallback_policy: str
    checks: tuple[DoctorCheck, ...]
    duration_ms: int

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "platform": self.platform,
            "cwd": self.cwd,
            "sandbox_backend": self.sandbox_backend,
            "fallback_policy": self.fallback_policy,
            "checks": [c.to_dict() for c in self.checks],
            "duration_ms": self.duration_ms,
        }


def _check_permissions(data_dir: Path) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    if not data_dir.exists():
        checks.append(
            DoctorCheck(
                id="permissions.data_dir",
                category="permissions",
                status="pass",
                summary=f"Runtime data directory will be created privately on first run ({data_dir})",
            )
        )
        return checks

    try:
        mode = stat.S_IMODE(data_dir.stat().st_mode)
    except OSError as exc:
        checks.append(
            DoctorCheck(
                id="permissions.data_dir",
                category="permissions",
                status="fail",
                summary=f"Cannot stat runtime data directory: {data_dir}",
                detail=str(exc),
                code="DATA_DIR_UNREADABLE",
            )
        )
        return checks

    if mode & 0o077:
        checks.append(
            DoctorCheck(
                id="permissions.data_dir",
                category="permissions",
                status="fail",
                summary=f"{data_dir} permissions are too open ({oct(mode)}, expected 0o700)",
                code="DATA_DIR_PERMISSIONS_LOOSE",
                remediation=f"chmod 700 {data_dir}",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                id="permissions.data_dir",
                category="permissions",
                status="pass",
                summary=f"{data_dir} permissions are private ({oct(mode)})",
            )
        )

    for rel_name in ("auth_token", "vault.json"):
        secret_file = data_dir / rel_name
        if not secret_file.exists():
            continue
        try:
            fmode = stat.S_IMODE(secret_file.stat().st_mode)
        except OSError as exc:
            checks.append(
                DoctorCheck(
                    id=f"permissions.{rel_name}",
                    category="permissions",
                    status="fail",
                    summary=f"Cannot inspect {secret_file}",
                    detail=str(exc),
                )
            )
            continue
        if fmode & 0o077:
            checks.append(
                DoctorCheck(
                    id=f"permissions.{rel_name}",
                    category="permissions",
                    status="fail",
                    summary=f"{secret_file} permissions are too open ({oct(fmode)}, expected 0o600)",
                    code="DATA_DIR_PERMISSIONS_LOOSE",
                    remediation=f"chmod 600 {secret_file}",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    id=f"permissions.{rel_name}",
                    category="permissions",
                    status="pass",
                    summary=f"{rel_name} permissions are private ({oct(fmode)})",
                )
            )
    return checks


def _check_audit_chain(data_dir: Path) -> DoctorCheck:
    audit_path = data_dir / "audit.jsonl"
    if not audit_path.is_file():
        return DoctorCheck(
            id="config.audit_chain",
            category="config",
            status="skip",
            summary="No audit.jsonl present yet (chain will initialize on first tool call)",
        )
    anchor = None
    anchor_path = audit_path.with_name(audit_path.name + ".head")
    if anchor_path.is_file():
        try:
            parsed = json.loads(anchor_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                anchor = parsed
        except (OSError, json.JSONDecodeError):
            anchor = None
    verification = verify_chain(
        audit_path,
        namespace=_AUDIT_CHAIN_NAMESPACE,
        anchor=anchor,
    )
    if verification.ok:
        return DoctorCheck(
            id="config.audit_chain",
            category="config",
            status="pass",
            summary=f"Audit hash chain verified ({verification.records} entries)",
        )
    return DoctorCheck(
        id="config.audit_chain",
        category="config",
        status="fail",
        summary=f"Audit hash chain integrity failure ({verification.failure or 'corrupted'})",
        detail=f"{audit_path}: {verification.failure or 'hash mismatch'}",
        code="AUDIT_CHAIN_BROKEN",
        remediation=f"Inspect {audit_path} for tampering or truncation.",
    )


def check_sandbox_health(
    *,
    platform: str | None = None,
    bwrap_availability: BwrapAvailability | None = None,
    seatbelt_available: bool | None = None,
) -> SandboxHealthReport:
    """Inspect OS sandbox backend readiness without running containment probes."""
    active_platform = platform or sys.platform
    checks: list[DoctorCheck] = []

    if active_platform == "darwin":
        sb_ok = (
            (os.path.isfile(_SANDBOX_EXECUTABLE) and os.access(_SANDBOX_EXECUTABLE, os.X_OK))
            if seatbelt_available is None
            else seatbelt_available
        )
        if sb_ok:
            checks.append(
                DoctorCheck(
                    id="sandbox.seatbelt.binary",
                    category="sandbox",
                    status="pass",
                    summary=f"macOS Seatbelt ready ({_SANDBOX_EXECUTABLE})",
                )
            )
            return SandboxHealthReport(
                platform=active_platform,
                backend="seatbelt",
                ready=True,
                binary_path=_SANDBOX_EXECUTABLE,
                binary_version=None,
                checks=tuple(checks),
            )
        checks.append(
            DoctorCheck(
                id="sandbox.seatbelt.binary",
                category="sandbox",
                status="fail",
                summary=f"{_SANDBOX_EXECUTABLE} is missing or not executable",
                code="SEATBELT_MISSING",
                remediation="Verify macOS system integrity (SIP) and /usr/bin/sandbox-exec permissions.",
            )
        )
        return SandboxHealthReport(
            platform=active_platform,
            backend="unavailable",
            ready=False,
            binary_path=None,
            binary_version=None,
            checks=tuple(checks),
        )

    if active_platform == "linux":
        avail = (
            bwrap_availability
            if bwrap_availability is not None
            else detect_bwrap_availability()
        )
        if avail.ready:
            checks.append(
                DoctorCheck(
                    id="sandbox.bwrap.ready",
                    category="sandbox",
                    status="pass",
                    summary=(
                        f"Linux bubblewrap ready ({avail.binary_path}"
                        f"{f' v{avail.version}' if avail.version else ''})"
                    ),
                )
            )
            if avail.version_warning:
                checks.append(
                    DoctorCheck(
                        id="sandbox.bwrap.version",
                        category="sandbox",
                        status="warn",
                        summary=avail.version_warning,
                    )
                )
            return SandboxHealthReport(
                platform=active_platform,
                backend="bwrap",
                ready=True,
                binary_path=avail.binary_path,
                binary_version=avail.version,
                checks=tuple(checks),
            )

        checks.append(
            DoctorCheck(
                id="sandbox.bwrap.ready",
                category="sandbox",
                status="fail",
                summary=avail.reason or "bubblewrap sandbox is unavailable",
                code=avail.code or "BWRAP_UNAVAILABLE",
                remediation=avail.remediation,
            )
        )
        return SandboxHealthReport(
            platform=active_platform,
            backend="unavailable",
            ready=False,
            binary_path=avail.binary_path,
            binary_version=avail.version,
            checks=tuple(checks),
        )

    checks.append(
        DoctorCheck(
            id="sandbox.platform",
            category="sandbox",
            status="fail",
            summary=f"No native OS sandbox is implemented for platform '{active_platform}'",
            code="SANDBOX_PLATFORM_UNSUPPORTED",
            remediation="Run on macOS, Linux, or WSL2.",
        )
    )
    return SandboxHealthReport(
        platform=active_platform,
        backend="unavailable",
        ready=False,
        binary_path=None,
        binary_version=None,
        checks=tuple(checks),
    )


async def _run_live_probes(
    *,
    platform: str,
    bwrap_availability: BwrapAvailability | None = None,
    seatbelt_available: bool | None = None,
) -> list[DoctorCheck]:
    """Execute 8 live containment probes inside a disposable temporary sandbox."""
    checks: list[DoctorCheck] = []
    canary_secret = "SMITH_DOCTOR_CANARY_982341"
    canary_env_key = "SMITH_DOCTOR_CANARY_SECRET"

    with tempfile.TemporaryDirectory(prefix="smith-doctor-probe-") as tmp_str:
        tmp = Path(tmp_str).resolve()
        ws = tmp / "workspace"
        outside = tmp / "outside"
        runtime_secret_dir = tmp / "runtime_secret"
        ro_tools = tmp / "ro_tools"
        non_delegable_dir = ws / "agents" / "tools"
        for d in (ws, outside, runtime_secret_dir, ro_tools, non_delegable_dir):
            d.mkdir(parents=True, exist_ok=True)

        # Plant isolated probe fixtures (never touches real user files!)
        (ws / ".env").write_text(f"KEY={canary_secret}\n", encoding="utf-8")
        (ws / ".ssh").mkdir()
        (ws / ".ssh" / "id_ed25519").write_text(f"SSH={canary_secret}\n", encoding="utf-8")
        (ws / ".git").mkdir()
        (ws / ".git" / "config").write_text(f"GIT={canary_secret}\n", encoding="utf-8")
        rt_cfg = runtime_secret_dir / "config.yaml"
        rt_cfg.write_text(f"api_key: {canary_secret}\n", encoding="utf-8")
        rt_vault = runtime_secret_dir / "vault.json"
        rt_vault.write_text(f'{{"k": "{canary_secret}"}}\n', encoding="utf-8")
        nd_file = non_delegable_dir / "dummy.py"
        nd_file.write_text("ORIGINAL_TOOL_CODE\n", encoding="utf-8")
        (ro_tools / "probe.txt").write_text("TOOLCHAIN_RO_OK\n", encoding="utf-8")

        probe_cfg = SandboxProfileConfig(
            backend="auto",
            fallback_policy="deny",
            auto_detect_toolchains=True,
            auto_detect_caches=False,
            share_read_only=(str(ro_tools),),
            default_network="loopback_only",
        )
        profile = resolve_sandbox_profile(
            ws,
            probe_cfg,
            runtime_secret_paths=[rt_cfg, rt_vault],
            non_delegable_write_paths=[non_delegable_dir],
            platform=platform,
        )
        env = create_sandbox_environment(
            profile,
            platform=platform,
            bwrap_availability=bwrap_availability,
            seatbelt_available=seatbelt_available,
        )

        # Probe 1: workspace RW
        r1 = await env.run_command(
            command="printf 'hello-sandbox' > hello.txt && /bin/cat hello.txt",
            cwd=str(ws),
            timeout_seconds=10,
        )
        if r1.exit_code == 0 and r1.stdout == "hello-sandbox":
            checks.append(
                DoctorCheck("probe.workspace_rw", "probe", "pass", "Workspace read/write works inside sandbox")
            )
        else:
            checks.append(
                DoctorCheck(
                    "probe.workspace_rw",
                    "probe",
                    "fail",
                    "Workspace read/write probe failed",
                    detail=r1.error or r1.stderr or f"exit={r1.exit_code}",
                    code="PROBE_WORKSPACE_RW_FAILED",
                )
            )

        # Probe 2: outside write blocked
        escape_file = outside / "escape.txt"
        r2 = await env.run_command(
            command=f"printf escape > '{escape_file}'",
            cwd=str(ws),
            timeout_seconds=10,
        )
        if r2.exit_code != 0 and not escape_file.exists():
            checks.append(
                DoctorCheck("probe.outside_write_blocked", "probe", "pass", "Writes outside workspace are blocked")
            )
        else:
            checks.append(
                DoctorCheck(
                    "probe.outside_write_blocked",
                    "probe",
                    "fail",
                    "Sandbox failed to block write outside workspace",
                    code="PROBE_OUTSIDE_WRITE_ESCAPED",
                )
            )

        # Probe 3: secret files & runtime secrets blocked
        secret_targets = [
            ws / ".env",
            ws / ".ssh" / "id_ed25519",
            ws / ".git" / "config",
            rt_cfg,
            rt_vault,
        ]
        leaked_targets: list[str] = []
        for target in secret_targets:
            r3 = await env.run_command(
                argv=["/bin/cat", str(target)],
                cwd=str(ws),
                timeout_seconds=10,
            )
            if r3.exit_code == 0 or canary_secret in r3.stdout:
                leaked_targets.append(target.name)
        if not leaked_targets:
            checks.append(
                DoctorCheck(
                    "probe.secret_files_blocked",
                    "probe",
                    "pass",
                    "Credential files (.env, .ssh, .git/config, runtime config/vault) are blocked",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    "probe.secret_files_blocked",
                    "probe",
                    "fail",
                    f"Sandbox allowed reading protected secret(s): {', '.join(leaked_targets)}",
                    code="PROBE_SECRET_LEAKED",
                )
            )

        # Probe 4: non_delegable_write_paths remain read-only
        r4 = await env.run_command(
            command="printf 'MUTATED' > agents/tools/dummy.py",
            cwd=str(ws),
            timeout_seconds=10,
        )
        if r4.exit_code != 0 and nd_file.read_text(encoding="utf-8") == "ORIGINAL_TOOL_CODE\n":
            checks.append(
                DoctorCheck("probe.non_delegable_ro", "probe", "pass", "Non-delegable write roots are read-only")
            )
        else:
            checks.append(
                DoctorCheck(
                    "probe.non_delegable_ro",
                    "probe",
                    "fail",
                    "Sandbox allowed mutating a non-delegable write root",
                    code="PROBE_NON_DELEGABLE_MUTATED",
                )
            )

        # Probe 5: hardlink preflight
        alias = ws / "hardlink-alias.txt"
        os.link(ws / ".env", alias)
        try:
            r5 = await env.run_command(
                argv=["/bin/cat", "hardlink-alias.txt"],
                cwd=str(ws),
                timeout_seconds=10,
            )
        finally:
            alias.unlink(missing_ok=True)
        if r5.exit_code is None and r5.error and "hard links" in r5.error:
            checks.append(
                DoctorCheck("probe.hardlink_preflight", "probe", "pass", "Hardlink alias preflight rejects secret aliases")
            )
        else:
            checks.append(
                DoctorCheck(
                    "probe.hardlink_preflight",
                    "probe",
                    "fail",
                    "Hardlink preflight did not reject .env alias",
                    detail=r5.error or r5.stderr or f"exit={r5.exit_code}",
                    code="PROBE_HARDLINK_FAILED",
                )
            )

        # Probe 6: parent environment scrubbed
        prev_val = os.environ.get(canary_env_key)
        os.environ[canary_env_key] = canary_secret
        try:
            r6 = await env.run_command(
                argv=["/usr/bin/env"],
                cwd=str(ws),
                timeout_seconds=10,
            )
        finally:
            if prev_val is None:
                os.environ.pop(canary_env_key, None)
            else:
                os.environ[canary_env_key] = prev_val
        if r6.exit_code == 0 and canary_secret not in r6.stdout and canary_env_key not in r6.stdout:
            checks.append(
                DoctorCheck("probe.env_scrubbed", "probe", "pass", "Parent process secret environment variables are scrubbed")
            )
        else:
            checks.append(
                DoctorCheck(
                    "probe.env_scrubbed",
                    "probe",
                    "fail",
                    "Parent environment canary leaked into sandboxed command",
                    code="PROBE_ENV_LEAKED",
                )
            )

        # Probe 7: network loopback-only (127.0.0.1 works, RFC 5737 192.0.2.1:80 blocked)
        net_script = (
            "import socket, threading\n"
            "srv = socket.socket()\n"
            "srv.bind(('127.0.0.1', 0))\n"
            "port = srv.getsockname()[1]\n"
            "srv.listen(1)\n"
            "def accept():\n"
            "    c, _ = srv.accept()\n"
            "    c.sendall(b'ok')\n"
            "    c.close()\n"
            "t = threading.Thread(target=accept)\n"
            "t.start()\n"
            "cli = socket.create_connection(('127.0.0.1', port), timeout=2)\n"
            "assert cli.recv(2) == b'ok'\n"
            "cli.close(); t.join(); srv.close()\n"
            "print('LOOPBACK_OK')\n"
            "try:\n"
            "    ext = socket.create_connection(('192.0.2.1', 80), timeout=1)\n"
            "    ext.close()\n"
            "    print('OUTBOUND_LEAKED')\n"
            "except OSError as exc:\n"
            "    print(f'OUTBOUND_BLOCKED:{exc.errno}')\n"
        )
        r7 = await env.run_command(
            argv=[sys.executable, "-c", net_script],
            cwd=str(ws),
            timeout_seconds=10,
        )
        if (
            r7.exit_code == 0
            and "LOOPBACK_OK" in r7.stdout
            and "OUTBOUND_BLOCKED" in r7.stdout
            and "OUTBOUND_LEAKED" not in r7.stdout
        ):
            checks.append(
                DoctorCheck(
                    "probe.network_loopback_only",
                    "probe",
                    "pass",
                    "Loopback networking works while outbound network is blocked",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    "probe.network_loopback_only",
                    "probe",
                    "fail",
                    "Loopback-only network isolation probe failed",
                    detail=(r7.error or r7.stderr or r7.stdout or f"exit={r7.exit_code}")[:300],
                    code="PROBE_NETWORK_FAILED",
                )
            )

        # Probe 8: share_read_only readable & non-writable
        ro_target = ro_tools / "probe.txt"
        ro_mutated = ro_tools / "mutated.txt"
        r8_read = await env.run_command(
            argv=["/bin/cat", str(ro_target)],
            cwd=str(ws),
            timeout_seconds=10,
        )
        r8_write = await env.run_command(
            command=f"printf nope > '{ro_mutated}'",
            cwd=str(ws),
            timeout_seconds=10,
        )
        if (
            r8_read.exit_code == 0
            and "TOOLCHAIN_RO_OK" in r8_read.stdout
            and r8_write.exit_code != 0
            and not ro_mutated.exists()
        ):
            checks.append(
                DoctorCheck(
                    "probe.toolchain_ro",
                    "probe",
                    "pass",
                    "Read-only shared directories are readable and protected against writes",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    "probe.toolchain_ro",
                    "probe",
                    "fail",
                    "Read-only share containment probe failed",
                    detail=r8_read.error or r8_read.stderr or r8_write.stderr,
                    code="PROBE_TOOLCHAIN_RO_FAILED",
                )
            )

    return checks


async def run_doctor_async(
    cwd: str | Path | None = None,
    *,
    run_probes: bool = True,
    data_dir: Path | None = None,
    platform: str | None = None,
    bwrap_availability: BwrapAvailability | None = None,
    seatbelt_available: bool | None = None,
) -> DoctorReport:
    """Run all static checks and (optionally) 8 live sandbox containment probes."""
    started = time.monotonic()
    active_platform = platform or sys.platform
    resolved_cwd = Path(cwd).expanduser().resolve() if cwd else Path.cwd().resolve()
    resolved_data = (
        data_dir if data_dir is not None else Path.home() / ".agent-smith"
    ).expanduser()

    checks: list[DoctorCheck] = []
    checks.extend(_check_permissions(resolved_data))

    # Load and validate ~/.agent-smith/config.yaml sandbox configuration
    sandbox_cfg = SandboxProfileConfig()
    profile: ResolvedSandboxProfile | None = None
    config_path = resolved_data / "config.yaml"
    try:
        raw_cfg = load_yaml(config_path) if config_path.is_file() else {}
        if not isinstance(raw_cfg, dict):
            raise ValueError("config.yaml root must be a YAML mapping")
        sandbox_cfg = SandboxProfileConfig.from_mapping(raw_cfg.get("sandbox"))
        if resolved_cwd.is_dir():
            profile = resolve_sandbox_profile(
                resolved_cwd,
                sandbox_cfg,
                data_dir=resolved_data,
                platform=active_platform,
            )
            if profile.rejected_paths:
                checks.append(
                    DoctorCheck(
                        id="config.sandbox",
                        category="config",
                        status="fail",
                        summary=f"Sandbox config contains {len(profile.rejected_paths)} rejected path(s)",
                        detail="; ".join(profile.rejected_paths),
                        code="SANDBOX_CONFIG_PATH_REJECTED",
                        remediation=f"Remove or narrow rejected paths in {config_path} under `sandbox:`.",
                    )
                )
            elif profile.warnings:
                checks.append(
                    DoctorCheck(
                        id="config.sandbox",
                        category="config",
                        status="warn",
                        summary="Sandbox config loaded with skipped paths",
                        detail="; ".join(profile.warnings),
                    )
                )
            else:
                checks.append(
                    DoctorCheck(
                        id="config.sandbox",
                        category="config",
                        status="pass",
                        summary=(
                            f"Sandbox config valid (backend={sandbox_cfg.backend}, "
                            f"fallback={sandbox_cfg.fallback_policy}, "
                            f"extra_roots={len(profile.extra_roots)}, "
                            f"share_ro={len(profile.share_read_only)}, "
                            f"caches={len(profile.cache_mounts)})"
                        ),
                    )
                )
    except (YamlConfigError, ValueError) as exc:
        checks.append(
            DoctorCheck(
                id="config.sandbox",
                category="config",
                status="fail",
                summary="Invalid sandbox configuration in config.yaml",
                detail=str(exc),
                code="SANDBOX_CONFIG_INVALID",
                remediation=f"Fix the `sandbox:` section in {config_path}.",
            )
        )

    checks.append(_check_audit_chain(resolved_data))

    health = check_sandbox_health(
        platform=active_platform,
        bwrap_availability=bwrap_availability,
        seatbelt_available=seatbelt_available,
    )
    checks.extend(health.checks)

    effective_backend = health.backend
    if not health.ready and sandbox_cfg.fallback_policy == "host_with_approval":
        effective_backend = "host_degraded"
        checks.append(
            DoctorCheck(
                id="sandbox.fallback",
                category="sandbox",
                status="warn",
                summary="Explicit fallback_policy=host_with_approval is active (NO OS SANDBOX; every shell call is CRITICAL)",
            )
        )

    if run_probes:
        if health.ready:
            probe_checks = await _run_live_probes(
                platform=active_platform,
                bwrap_availability=bwrap_availability,
                seatbelt_available=seatbelt_available,
            )
            checks.extend(probe_checks)
        else:
            checks.append(
                DoctorCheck(
                    id="probe.skipped",
                    category="probe",
                    status="skip",
                    summary="Live containment probes skipped because OS sandbox backend is not ready",
                )
            )

    duration_ms = int((time.monotonic() - started) * 1000)
    ok = not any(c.status == "fail" for c in checks)
    return DoctorReport(
        ok=ok,
        platform=active_platform,
        cwd=str(resolved_cwd),
        sandbox_backend=effective_backend,
        fallback_policy=sandbox_cfg.fallback_policy,
        checks=tuple(checks),
        duration_ms=duration_ms,
    )


def run_doctor(
    cwd: str | Path | None = None,
    *,
    run_probes: bool = True,
    data_dir: Path | None = None,
    platform: str | None = None,
    bwrap_availability: BwrapAvailability | None = None,
    seatbelt_available: bool | None = None,
) -> DoctorReport:
    """Synchronous convenience entry point for CLI and tests."""
    return asyncio.run(
        run_doctor_async(
            cwd=cwd,
            run_probes=run_probes,
            data_dir=data_dir,
            platform=platform,
            bwrap_availability=bwrap_availability,
            seatbelt_available=seatbelt_available,
        )
    )


def format_doctor_report(report: DoctorReport) -> str:
    """Render a concise, human-readable terminal report."""
    badge = {
        "pass": "[PASS]",
        "warn": "[WARN]",
        "fail": "[FAIL]",
        "skip": "[SKIP]",
    }
    lines = [
        f"Agent-Smith Doctor ({report.platform} · sandbox={report.sandbox_backend} · {report.duration_ms}ms)",
        f"Workspace: {report.cwd}",
        "",
    ]
    for check in report.checks:
        code_suffix = f" ({check.code})" if check.code else ""
        lines.append(f"  {badge[check.status]} {check.id}: {check.summary}{code_suffix}")
        if check.detail:
            lines.append(f"         detail: {check.detail}")
        if check.remediation:
            lines.append("         fix:")
            for rline in check.remediation.splitlines():
                lines.append(f"           {rline}")
    lines.append("")
    passed = sum(1 for c in report.checks if c.status == "pass")
    warned = sum(1 for c in report.checks if c.status == "warn")
    failed = sum(1 for c in report.checks if c.status == "fail")
    lines.append(
        f"Summary: {'OK' if report.ok else 'FAILED'} — {passed} passed, {warned} warning(s), {failed} failed"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m engine.sandbox.doctor",
        description="Run Agent-Smith environment and sandbox containment diagnostics.",
    )
    parser.add_argument("--cwd", default=None, help="Workspace directory to validate")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON output")
    parser.add_argument("--no-probe", action="store_true", help="Skip live sandbox containment probes")
    args = parser.parse_args(argv)

    report = run_doctor(cwd=args.cwd, run_probes=not args.no_probe)
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(format_doctor_report(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
