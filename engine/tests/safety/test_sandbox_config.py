"""Tests for SandboxProfileConfig, policy validation, and multi-root Seatbelt."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from common.sensitive_rules import runtime_credential_paths
from engine.safety.tool_guard import FileGuard
from engine.sandbox import (
    MacOSSeatbeltEnvironment,
    SandboxProfileConfig,
    resolve_sandbox_profile,
)


def test_sandbox_profile_config_rejects_unknown_keys_and_invalid_values() -> None:
    with pytest.raises(ValueError, match="unknown sandbox configuration keys"):
        SandboxProfileConfig.from_mapping({"bogus": True})
    with pytest.raises(ValueError, match="sandbox.backend"):
        SandboxProfileConfig.from_mapping({"backend": "docker"})
    with pytest.raises(ValueError, match="sandbox.fallback_policy"):
        SandboxProfileConfig.from_mapping({"fallback_policy": "allow_all"})
    with pytest.raises(ValueError, match="control characters"):
        SandboxProfileConfig.from_mapping({"extra_roots": ["/tmp/bad\npath"]})


def test_resolve_sandbox_profile_rejects_broad_and_credential_roots(
    tmp_path: Path,
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    ws = fake_home / "project"
    ws.mkdir()
    ssh_dir = fake_home / ".ssh"
    ssh_dir.mkdir()
    cfg_dir = fake_home / ".config"
    cfg_dir.mkdir()
    gh_dir = cfg_dir / "gh"
    gh_dir.mkdir()
    data_dir = fake_home / ".agent-smith"
    (data_dir / "builtin" / "skills").mkdir(parents=True)
    (data_dir / "config.yaml").write_text("llm: {}\n", encoding="utf-8")

    # Symlink masquerading as a normal folder pointing at .ssh
    sneaky = fake_home / "innocent-link"
    sneaky.symlink_to(ssh_dir)

    extra_ok = fake_home / "shared-lib"
    extra_ok.mkdir()
    cache_ok = fake_home / "my-uv-cache"
    cache_ok.mkdir()

    cfg = SandboxProfileConfig.from_mapping({
        "auto_detect_toolchains": False,
        "auto_detect_caches": False,
        "extra_roots": [
            "/",
            str(fake_home),
            str(ssh_dir),
            str(cfg_dir),
            str(gh_dir),
            str(data_dir),
            str(sneaky),
            str(extra_ok),
        ],
        "share_read_only": [
            str(data_dir),
            str(data_dir / "builtin" / "skills"),
        ],
        "cache_roots": {
            "uv": str(cache_ok),
            "pip": str(ssh_dir),
        },
    })
    profile = resolve_sandbox_profile(
        ws,
        cfg,
        home=fake_home,
        data_dir=data_dir,
    )

    assert profile.extra_roots == (extra_ok.resolve(),)
    # ~/.agent-smith root is rejected, while ~/.agent-smith/builtin/skills is allowed RO
    assert profile.share_read_only == ((data_dir / "builtin" / "skills").resolve(),)
    assert [m.kind for m in profile.cache_mounts] == ["uv"]
    assert profile.cache_env_vars()["UV_CACHE_DIR"] == str(cache_ok.resolve())
    assert len(profile.rejected_paths) >= 7


def test_file_guard_accepts_extra_roots_without_cache_roots(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    (ws / "a.txt").write_text("a\n")
    (extra / "b.txt").write_text("b\n")
    (cache / "c.txt").write_text("c\n")

    guard = FileGuard()
    guard.set_working_directory(ws, extra_roots=[extra])

    assert guard.check_path(str(ws / "a.txt"), writing=True).allowed
    assert guard.check_path(str(extra / "b.txt"), writing=True).allowed
    # Cache roots are NOT added to FileGuard._allowed
    cache_res = guard.check_path(str(cache / "c.txt"), writing=True)
    assert not cache_res.allowed
    assert cache_res.boundary_block


def test_sandbox_profile_defaults_are_fail_closed_no_caches_no_network(tmp_path: Path) -> None:
    """H2 & H3: default config disables auto_detect_caches and sets default_network='none'."""
    fake_home = tmp_path / "home"
    ws = fake_home / "ws"
    uv_cache = fake_home / ".cache" / "uv"
    cargo_reg = fake_home / ".cargo" / "registry"
    for d in (ws, uv_cache, cargo_reg):
        d.mkdir(parents=True)

    cfg = SandboxProfileConfig.from_mapping({})
    assert cfg.auto_detect_caches is False
    assert cfg.default_network == "none"

    profile = resolve_sandbox_profile(ws, cfg, home=fake_home, data_dir=fake_home / ".agent-smith")
    assert profile.cache_mounts == ()
    assert profile.default_network == "none"


def test_hardlink_preflight_blocks_external_link_even_when_shared_with_cache(
    tmp_path: Path,
) -> None:
    """H3: A workspace file linked into both a mounted cache AND an external file must fail closed."""
    from engine.sandbox.preflight import check_hardlinks

    ws = tmp_path / "ws"
    cache = tmp_path / "cache"
    outside = tmp_path / "outside"
    for d in (ws, cache, outside):
        d.mkdir()
    secret_outside = outside / "host_secret.txt"
    secret_outside.write_text("TOP_SECRET\n", encoding="utf-8")
    os.link(secret_outside, cache / "cached_blob")
    os.link(secret_outside, ws / "innocent.txt")

    err = check_hardlinks((ws,), (), cache_roots=(cache,))
    assert err is not None and "hard links" in err


def test_runtime_credential_paths_include_vault_and_auth_token(tmp_path: Path) -> None:
    paths = runtime_credential_paths(tmp_path)
    names = {p.name for p in paths}
    assert "vault.json" in names
    assert "auth_token" in names
    assert "config.yaml" in names


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_macos_seatbelt_supports_extra_roots_share_ro_and_cache_mounts(
    tmp_path: Path,
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    ws = fake_home / "ws"
    ws.mkdir()
    extra = fake_home / "extra"
    extra.mkdir()
    (extra / ".env").write_text("EXTRA_SECRET=nope\n", encoding="utf-8")
    ro = fake_home / "ro-tools"
    ro.mkdir()
    (ro / "tool.txt").write_text("readonly-tool\n", encoding="utf-8")
    cache = fake_home / "uv-cache"
    cache.mkdir()

    cfg = SandboxProfileConfig(
        auto_detect_toolchains=False,
        auto_detect_caches=False,
        extra_roots=(str(extra),),
        share_read_only=(str(ro),),
        cache_roots={"uv": str(cache)},
    )
    profile = resolve_sandbox_profile(ws, cfg, home=fake_home, data_dir=fake_home / ".agent-smith")
    env = MacOSSeatbeltEnvironment(workspace=ws, profile=profile)

    # Extra root is RW, but its .env is still blocked
    rw_res = asyncio.run(
        env.run_command(
            command=f"printf ok > '{extra}/out.txt'",
            cwd=str(ws),
        )
    )
    assert rw_res.exit_code == 0, rw_res.stderr or rw_res.error
    assert (extra / "out.txt").read_text() == "ok"

    env_res = asyncio.run(
        env.run_command(argv=["/bin/cat", str(extra / ".env")], cwd=str(ws))
    )
    assert env_res.exit_code not in (None, 0)
    assert "EXTRA_SECRET" not in env_res.stdout

    # share_read_only is readable but not writable
    ro_read = asyncio.run(
        env.run_command(argv=["/bin/cat", str(ro / "tool.txt")], cwd=str(ws))
    )
    assert ro_read.exit_code == 0
    assert ro_read.stdout == "readonly-tool\n"

    ro_write = asyncio.run(
        env.run_command(command=f"printf bad > '{ro}/mutated.txt'", cwd=str(ws))
    )
    assert ro_write.exit_code not in (None, 0)
    assert not (ro / "mutated.txt").exists()

    # cache_roots is RW and UV_CACHE_DIR is injected
    cache_res = asyncio.run(
        env.run_command(
            command='printf cached > "$UV_CACHE_DIR/item.bin" && /bin/cat "$UV_CACHE_DIR/item.bin"',
            cwd=str(ws),
        )
    )
    assert cache_res.exit_code == 0, cache_res.stderr or cache_res.error
    assert cache_res.stdout == "cached"

    # Hardlink inside extra_root pointing at extra_root/.env is rejected by preflight
    os.link(extra / ".env", extra / "alias.txt")
    hl_res = asyncio.run(env.run_command(argv=["/bin/echo", "hi"], cwd=str(ws)))
    assert hl_res.exit_code is None
    assert hl_res.error and "hard links" in hl_res.error
