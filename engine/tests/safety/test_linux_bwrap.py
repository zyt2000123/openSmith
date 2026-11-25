"""Tests for Linux bubblewrap backend, factory routing, and shell network gating."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from engine.safety.approval import ApprovalScope, build_approval_presentation
from engine.safety.risk import RiskTier
from engine.safety.tool_guard import ToolGuard, call_requests_network_egress
from engine.sandbox import (
    BwrapAvailability,
    CommandResult,
    DegradedHostSandboxEnvironment,
    LinuxBwrapEnvironment,
    SandboxProfileConfig,
    UnavailableSandboxEnvironment,
    create_sandbox_environment,
    detect_bwrap_availability,
    probe_bwrap_availability,
    resolve_sandbox_profile,
)
from engine.tool.interface import ToolCall, ToolDefinition

ROOT = Path(__file__).resolve().parents[3]


def _load_shell_tool():
    path = ROOT / "agents" / "tools" / "shell.py"
    spec = importlib.util.spec_from_file_location("shell_tool_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bwrap_argv_enforces_seven_stage_mount_order_and_flags(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    ws = fake_home / "project"
    extra = fake_home / "shared"
    ro = fake_home / "toolchain"
    cache = fake_home / "uv-cache"
    provider = ws / "agents" / "tools"
    for d in (ws, extra, ro, cache, provider):
        d.mkdir(parents=True)
    (ws / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("[core]\n", encoding="utf-8")

    cfg = SandboxProfileConfig(
        auto_detect_toolchains=False,
        auto_detect_caches=False,
        extra_roots=(str(extra),),
        share_read_only=(str(ro),),
        cache_roots={"uv": str(cache)},
    )
    profile = resolve_sandbox_profile(
        ws,
        cfg,
        non_delegable_write_paths=[provider],
        home=fake_home,
        data_dir=fake_home / ".agent-smith",
        platform="linux",
    )
    env = LinuxBwrapEnvironment(
        workspace=ws,
        profile=profile,
        bwrap_binary="/usr/bin/bwrap",
        host_home=fake_home,
    )
    sfile = tmp_path / "sfile"
    sdir = tmp_path / "sdir"
    sfile.write_bytes(b"")
    sdir.mkdir()

    safe_env = env._safe_environment(None)
    argv_offline = env._build_bwrap_argv(
        bwrap_bin="/usr/bin/bwrap",
        resolved_cwd=str(ws.resolve()),
        inner_argv=["/bin/sh", "-c", "echo ok"],
        env=safe_env,
        network=False,
        sentinel_file=sfile,
        sentinel_dir=sdir,
    )

    # Core security flags
    for flag in (
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--unshare-net",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
    ):
        assert flag in argv_offline

    # Stage ordering check:
    # Stage 1 (/etc, --tmpfs /run, resolv.conf) < Stage 2 (--tmpfs $HOME) < Stage 3 (ro)
    # < Stage 4 (cache) < Stage 5 (workspace/extra) < Stage 6 (sensitive blackhole)
    # < Stage 7 (non-delegable ro)
    def idx_pair(*tokens: str) -> int:
        for i in range(len(argv_offline) - len(tokens) + 1):
            if tuple(argv_offline[i : i + len(tokens)]) == tokens:
                return i
        raise AssertionError(f"Sequence not found: {tokens} in {argv_offline}")

    i_etc = idx_pair("--ro-bind", "/etc", "/etc")
    i_run = idx_pair("--tmpfs", "/run")
    i_home = idx_pair("--tmpfs", str(fake_home.resolve()))
    i_ro = idx_pair("--ro-bind", str(ro.resolve()), str(ro.resolve()))
    i_cache = idx_pair("--bind", str(cache.resolve()), str(cache.resolve()))
    i_ws = idx_pair("--bind", str(ws.resolve()), str(ws.resolve()))
    i_extra = idx_pair("--bind", str(extra.resolve()), str(extra.resolve()))
    i_env_blackhole = idx_pair("--ro-bind", str(sfile), str((ws / ".env").resolve()))
    i_git_blackhole = idx_pair("--ro-bind", str(sdir), str((ws / ".git").resolve()))
    i_non_delegable = idx_pair("--ro-bind", str(provider.resolve()), str(provider.resolve()))

    assert i_etc < i_run < i_home < i_ro < i_cache < i_ws < i_extra
    assert i_extra < i_env_blackhole < i_non_delegable
    assert i_extra < i_git_blackhole < i_non_delegable

    # Online mode omits --unshare-net, keeps ALL FS isolation!
    argv_online = env._build_bwrap_argv(
        bwrap_bin="/usr/bin/bwrap",
        resolved_cwd=str(ws.resolve()),
        inner_argv=["/bin/sh", "-c", "curl https://example.com"],
        env=safe_env,
        network=True,
        sentinel_file=sfile,
        sentinel_dir=sdir,
        resolv_is_symlink=False,
    )
    assert "--unshare-net" not in argv_online
    assert "/etc/resolv.conf" not in argv_online
    assert "--tmpfs" in argv_online
    assert str(fake_home.resolve()) in argv_online
    assert str((ws / ".env").resolve()) in argv_online
    assert str(provider.resolve()) in argv_online


def test_bwrap_handles_ubuntu_resolv_conf_symlink_and_smoke_test_uses_real_mounts(
    tmp_path: Path,
) -> None:
    """H4: Ubuntu /etc/resolv.conf -> /run/systemd/resolve/stub-resolv.conf works in
    both offline and online modes, and smoke test exercises the real Stage 1 mounts."""
    fake_home = tmp_path / "home"
    ws = fake_home / "ws"
    ro = fake_home / "ro-share"
    for d in (ws, ro):
        d.mkdir(parents=True)
    # M4: secret placed inside share_read_only must also be blackholed by bwrap
    (ro / ".env").write_text("RO_SECRET=1\n", encoding="utf-8")

    cfg = SandboxProfileConfig(
        auto_detect_toolchains=False,
        auto_detect_caches=False,
        share_read_only=(str(ro),),
    )
    profile = resolve_sandbox_profile(
        ws, cfg, home=fake_home, data_dir=fake_home / ".agent-smith", platform="linux"
    )
    env = LinuxBwrapEnvironment(
        workspace=ws,
        profile=profile,
        bwrap_binary="/usr/bin/bwrap",
        host_home=fake_home,
    )
    sfile = tmp_path / "sfile"
    sdir = tmp_path / "sdir"
    sfile.write_bytes(b"")
    sdir.mkdir()

    stub_target = Path("/run/systemd/resolve/stub-resolv.conf")
    safe_env = env._safe_environment(None)

    def find_seq(argv: list[str], *tokens: str) -> int:
        for i in range(len(argv) - len(tokens) + 1):
            if tuple(argv[i : i + len(tokens)]) == tokens:
                return i
        raise AssertionError(f"Sequence not found: {tokens} in {argv}")

    # Offline Ubuntu: --tmpfs /run comes BEFORE --ro-bind /dev/null /run/systemd/resolve/stub-resolv.conf
    argv_off = env._build_bwrap_argv(
        bwrap_bin="/usr/bin/bwrap",
        resolved_cwd=str(ws.resolve()),
        inner_argv=["/bin/true"],
        env=safe_env,
        network=False,
        sentinel_file=sfile,
        sentinel_dir=sdir,
        resolv_is_symlink=True,
        resolv_real_target=stub_target,
    )
    i_run_off = find_seq(argv_off, "--tmpfs", "/run")
    i_null_stub = find_seq(argv_off, "--ro-bind", "/dev/null", str(stub_target))
    assert i_run_off < i_null_stub
    # M4: ro-share/.env is blackholed with sentinel_file after ro-share is mounted
    i_ro = find_seq(argv_off, "--ro-bind", str(ro.resolve()), str(ro.resolve()))
    i_ro_env = find_seq(argv_off, "--ro-bind", str(sfile), str((ro / ".env").resolve()))
    assert i_ro < i_ro_env

    # Online Ubuntu: --tmpfs /run comes BEFORE --ro-bind <stub> <stub> so DNS resolves
    argv_on = env._build_bwrap_argv(
        bwrap_bin="/usr/bin/bwrap",
        resolved_cwd=str(ws.resolve()),
        inner_argv=["/bin/true"],
        env=safe_env,
        network=True,
        sentinel_file=sfile,
        sentinel_dir=sdir,
        resolv_is_symlink=True,
        resolv_real_target=stub_target,
    )
    i_run_on = find_seq(argv_on, "--tmpfs", "/run")
    i_real_stub = find_seq(argv_on, "--ro-bind", str(stub_target), str(stub_target))
    assert i_run_on < i_real_stub

    # Smoke test in probe_bwrap_availability uses real Stage 1 mounts including resolv.conf
    captured_smoke: list[list[str]] = []

    def fake_smoke(argv: list[str]) -> tuple[int, str]:
        captured_smoke.append(argv)
        return 0, ""

    avail = probe_bwrap_availability(
        bwrap_path="/usr/bin/bwrap",
        unprivileged_userns_clone="1",
        apparmor_restrict_userns="0",
        in_container=False,
        run_smoke=fake_smoke,
        resolv_is_symlink=True,
        resolv_real_target=stub_target,
    )
    assert avail.ready
    assert captured_smoke
    smoke = captured_smoke[0]
    assert "--unshare-net" in smoke
    assert "--etc" not in smoke
    assert find_seq(smoke, "--ro-bind", "/etc", "/etc") < find_seq(smoke, "--tmpfs", "/run")
    assert find_seq(smoke, "--tmpfs", "/run") < find_seq(smoke, "--ro-bind", "/dev/null", str(stub_target))

    # L3: no sys.executable loopback bootstrap, always --cap-drop ALL, direct inner_argv
    assert sys.executable not in argv_off
    assert "--cap-drop" in argv_off and "ALL" in argv_off
    assert argv_off[-2:] == ["--", "/bin/true"]


def test_bwrap_sensitive_scan_is_fingerprint_cached_and_skips_cache_mounts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """M6: sensitive-path scan caches by directory fingerprint and skips cache_mounts."""
    from engine.sandbox import linux_bwrap as lb_module

    fake_home = tmp_path / "home"
    ws = fake_home / "ws"
    cache = fake_home / "uv-cache"
    for d in (ws, cache):
        d.mkdir(parents=True)
    (ws / ".env").write_text("WS_SECRET=1\n", encoding="utf-8")
    (cache / ".env").write_text("CACHE_NOT_SCANNED=1\n", encoding="utf-8")

    cfg = SandboxProfileConfig(
        auto_detect_toolchains=False,
        auto_detect_caches=False,
        cache_roots={"uv": str(cache)},
    )
    profile = resolve_sandbox_profile(
        ws, cfg, home=fake_home, data_dir=fake_home / ".agent-smith", platform="linux"
    )
    env = LinuxBwrapEnvironment(workspace=ws, profile=profile, host_home=fake_home)

    scan_calls = {"count": 0}
    real_scan = lb_module._find_sensitive_paths_under

    def counting_scan(roots, secrets):
        scan_calls["count"] += 1
        return real_scan(roots, secrets)

    monkeypatch.setattr(lb_module, "_find_sensitive_paths_under", counting_scan)

    first = env._cached_sensitive_paths()
    second = env._cached_sensitive_paths()
    assert scan_calls["count"] == 1
    assert any(p == (ws / ".env").resolve() for p, _ in first)
    assert not any(p == (cache / ".env").resolve() for p, _ in first)
    assert first == second

    # Mutating workspace invalidates fingerprint cache
    (ws / "new.txt").write_text("x\n", encoding="utf-8")
    env._cached_sensitive_paths()
    assert scan_calls["count"] == 2


def test_bwrap_availability_detects_missing_apparmor_and_userns_disabled() -> None:
    missing = probe_bwrap_availability(bwrap_path="")
    assert not missing.ready
    assert missing.code == "BWRAP_NOT_INSTALLED"
    assert missing.remediation

    apparmor = probe_bwrap_availability(
        bwrap_path="/usr/bin/bwrap",
        unprivileged_userns_clone="1",
        apparmor_restrict_userns="1",
        in_container=False,
        run_smoke=lambda _argv: (1, "bwrap: setting up uid map: Permission denied"),
    )
    assert not apparmor.ready
    assert apparmor.code == "LINUX_APPARMOR_USERNS_BLOCKED"
    assert "/etc/apparmor.d/bwrap" in (apparmor.remediation or "")

    userns_off = probe_bwrap_availability(
        bwrap_path="/usr/bin/bwrap",
        unprivileged_userns_clone="0",
        apparmor_restrict_userns="0",
        in_container=False,
        run_smoke=lambda _argv: (1, "Creating new namespace failed: Operation not permitted"),
    )
    assert not userns_off.ready
    assert userns_off.code == "LINUX_USERNS_DISABLED"

    container_blocked = probe_bwrap_availability(
        bwrap_path="/usr/bin/bwrap",
        unprivileged_userns_clone="1",
        apparmor_restrict_userns="0",
        in_container=True,
        run_smoke=lambda _argv: (1, "Creating new namespace failed: Operation not permitted"),
    )
    assert not container_blocked.ready
    assert container_blocked.code == "LINUX_CONTAINER_USERNS_BLOCKED"

    ready = probe_bwrap_availability(
        bwrap_path="/usr/bin/bwrap",
        unprivileged_userns_clone="1",
        apparmor_restrict_userns="1",
        in_container=False,
        run_smoke=lambda _argv: (0, ""),
    )
    assert ready.ready


def test_factory_fail_closed_vs_explicit_degraded_fallback(tmp_path: Path) -> None:
    profile_deny = resolve_sandbox_profile(
        tmp_path,
        SandboxProfileConfig(
            auto_detect_toolchains=False,
            auto_detect_caches=False,
            fallback_policy="deny",
        ),
        platform="linux",
    )
    unavailable = create_sandbox_environment(
        profile_deny,
        platform="linux",
        bwrap_availability=BwrapAvailability(
            ready=False,
            code="LINUX_APPARMOR_USERNS_BLOCKED",
            reason="AppArmor blocked userns",
            remediation="install profile",
        ),
    )
    assert isinstance(unavailable, UnavailableSandboxEnvironment)
    res = asyncio.run(unavailable.run_command("echo hi", cwd=str(tmp_path)))
    assert res.exit_code is None
    assert res.error and "LINUX_APPARMOR_USERNS_BLOCKED" in res.error

    profile_degraded = resolve_sandbox_profile(
        tmp_path,
        SandboxProfileConfig(
            auto_detect_toolchains=False,
            auto_detect_caches=False,
            fallback_policy="host_with_approval",
        ),
        platform="linux",
    )
    degraded = create_sandbox_environment(
        profile_degraded,
        platform="linux",
        bwrap_availability=BwrapAvailability(
            ready=False,
            code="BWRAP_NOT_INSTALLED",
            reason="missing bwrap",
        ),
    )
    assert isinstance(degraded, DegradedHostSandboxEnvironment)
    assert degraded.is_degraded is True


def test_shell_network_parameter_and_offline_hint(tmp_path: Path) -> None:
    shell = _load_shell_tool()

    class _FakeEnv:
        name = "sandbox"

        def __init__(self, result: CommandResult) -> None:
            self.result = result
            self.seen_network: bool | None = None

        async def run_command(self, command, *, cwd=None, timeout_seconds=30.0, env=None, network=False):
            self.seen_network = network
            return self.result

    offline_env = _FakeEnv(
        CommandResult(
            exit_code=6,
            stderr="curl: (6) Could not resolve host: example.com",
            network_denied_hint=True,
        )
    )
    out = asyncio.run(
        shell.execute(command="curl https://example.com", cwd=str(tmp_path), environment=offline_env)
    )
    assert offline_env.seen_network is False
    assert "[sandbox:network_denied]" in out
    assert "network=true" in out

    # Non-boolean network rejected
    bad = asyncio.run(
        shell.execute(
            command="echo hi",
            cwd=str(tmp_path),
            network="true",  # type: ignore[arg-type]
            environment=offline_env,
        )
    )
    assert "Error: network must be a boolean" in bad


def test_tool_guard_network_egress_helper_and_approval_presentation(tmp_path: Path) -> None:
    shell_def = ToolDefinition(
        name="shell",
        description="shell",
        opaque_command=True,
        permission_level="execute",
        approval_policy="always",
        execution_environment="sandbox",
    )
    fetch_def = ToolDefinition(
        name="web_fetch",
        description="fetch",
        network_access=True,
    )
    guard = ToolGuard(
        tmp_path / "missing.json",
        tool_registry={"shell": shell_def, "web_fetch": fetch_def},
    )

    c_offline = ToolCall(id="1", name="shell", arguments={"command": "pytest"})
    c_online = ToolCall(
        id="2", name="shell", arguments={"command": "curl https://example.com", "network": True}
    )
    c_fetch = ToolCall(id="3", name="web_fetch", arguments={"url": "https://example.com"})

    assert not call_requests_network_egress(c_offline, shell_def)
    assert call_requests_network_egress(c_online, shell_def)
    assert call_requests_network_egress(c_fetch, fetch_def)

    d_off = guard.check(c_offline, audit=False)
    d_on = guard.check(c_online, audit=False)
    assert d_off.approval_scope is not None
    assert d_off.approval_scope.kind == "sandbox_command"
    assert d_off.risk is RiskTier.ELEVATED

    assert d_on.approval_scope is not None
    assert d_on.approval_scope.kind == "sandbox_network"
    assert d_on.risk is RiskTier.HIGH

    pres = build_approval_presentation(
        "shell",
        "execute",
        d_on.reason,
        {"command": "curl https://example.com", "network": True},
        scope=d_on.approval_scope,
    )
    assert "outbound network" in pres.title.lower()
    assert any(d.label == "Outbound network" for d in pres.details)

    # H5: Degraded host_command + network=True MUST say NO OS SANDBOX, never "sandboxed"!
    host_scope = ApprovalScope.host_command("curl https://example.com", high_risk=True)
    pres_host_net = build_approval_presentation(
        "shell",
        "execute",
        "",
        {"command": "curl https://example.com", "network": True},
        scope=host_scope,
    )
    assert "NO OS SANDBOX" in pres_host_net.title
    assert "without OS sandbox" in pres_host_net.summary
    assert "filesystem sandbox remains active" not in pres_host_net.reason.lower()


@pytest.mark.skipif(
    sys.platform != "linux" or not detect_bwrap_availability().ready,
    reason="Requires Linux with working bubblewrap",
)
def test_linux_bwrap_live_containment(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    (ws / ".env").write_text("BWRAP_SECRET=nope\n", encoding="utf-8")

    env = LinuxBwrapEnvironment(workspace=ws)
    ok = asyncio.run(env.run_command("printf hi > hello.txt && cat hello.txt", cwd=str(ws)))
    assert ok.exit_code == 0, ok.stderr or ok.error
    assert ok.stdout == "hi"

    blocked_out = asyncio.run(
        env.run_command(f"printf bad > '{outside}'", cwd=str(ws))
    )
    assert blocked_out.exit_code != 0
    assert not outside.exists()

    blocked_env = asyncio.run(env.run_command("cat .env", cwd=str(ws)))
    assert blocked_env.exit_code != 0
    assert "BWRAP_SECRET" not in blocked_env.stdout
