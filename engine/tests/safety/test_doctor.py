"""Tests for engine.sandbox.doctor (static checks, 8 live probes, and CLI exit codes)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from common.hash_chain import HashChainLog
from engine.sandbox import BwrapAvailability, run_doctor
from engine.sandbox.doctor import format_doctor_report, main as doctor_main


def test_doctor_reports_bwrap_missing_and_apparmor_remediation(tmp_path: Path) -> None:
    data_dir = tmp_path / ".agent-smith"
    data_dir.mkdir(mode=0o700)

    missing = run_doctor(
        cwd=tmp_path,
        run_probes=True,
        data_dir=data_dir,
        platform="linux",
        bwrap_availability=BwrapAvailability(
            ready=False,
            code="BWRAP_NOT_INSTALLED",
            reason="bwrap not found",
            remediation="sudo apt install -y bubblewrap",
        ),
    )
    assert not missing.ok
    assert missing.sandbox_backend == "unavailable"
    by_id = {c.id: c for c in missing.checks}
    assert by_id["sandbox.bwrap.ready"].code == "BWRAP_NOT_INSTALLED"
    assert by_id["probe.skipped"].status == "skip"

    apparmor = run_doctor(
        cwd=tmp_path,
        run_probes=False,
        data_dir=data_dir,
        platform="linux",
        bwrap_availability=BwrapAvailability(
            ready=False,
            code="LINUX_APPARMOR_USERNS_BLOCKED",
            reason="AppArmor blocked userns",
            remediation="sudo tee /etc/apparmor.d/bwrap ...",
        ),
    )
    assert not apparmor.ok
    rendered = format_doctor_report(apparmor)
    assert "LINUX_APPARMOR_USERNS_BLOCKED" in rendered
    assert "/etc/apparmor.d/bwrap" in rendered


def test_doctor_detects_loose_permissions_rejected_config_and_broken_audit_chain(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / ".agent-smith"
    data_dir.mkdir(mode=0o755)
    os.chmod(data_dir, 0o755)
    token = data_dir / "auth_token"
    token.write_text("secret", encoding="utf-8")
    os.chmod(token, 0o644)

    # Config with rejected broad root `/`
    (data_dir / "config.yaml").write_text(
        "sandbox:\n  extra_roots:\n    - /\n",
        encoding="utf-8",
    )

    # Tampered audit hash chain
    audit_path = data_dir / "audit.jsonl"
    chain = HashChainLog(audit_path, namespace="agent-smith-audit")
    chain.append({"tool": "read_file", "allowed": True})
    chain.close()
    raw_lines = audit_path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(raw_lines[0])
    tampered["tool"] = "tampered_tool"
    audit_path.write_text(json.dumps(tampered) + "\n", encoding="utf-8")

    report = run_doctor(
        cwd=tmp_path,
        run_probes=False,
        data_dir=data_dir,
        platform="darwin",
        seatbelt_available=True,
    )
    assert not report.ok
    codes = {c.code for c in report.checks if c.code}
    assert "DATA_DIR_PERMISSIONS_LOOSE" in codes
    assert "SANDBOX_CONFIG_PATH_REJECTED" in codes
    assert "AUDIT_CHAIN_BROKEN" in codes


@pytest.mark.skipif(sys.platform != "darwin", reason="Live Seatbelt probes require macOS")
def test_doctor_live_eight_probes_pass_on_macos(tmp_path: Path) -> None:
    data_dir = tmp_path / ".agent-smith"
    data_dir.mkdir(mode=0o700)
    os.chmod(data_dir, 0o700)

    report = run_doctor(cwd=tmp_path, run_probes=True, data_dir=data_dir)
    assert report.ok, format_doctor_report(report)
    assert report.sandbox_backend == "seatbelt"
    probe_ids = {c.id for c in report.checks if c.category == "probe"}
    expected_probes = {
        "probe.workspace_rw",
        "probe.outside_write_blocked",
        "probe.secret_files_blocked",
        "probe.non_delegable_ro",
        "probe.hardlink_preflight",
        "probe.env_scrubbed",
        "probe.network_loopback_only",
        "probe.toolchain_ro",
    }
    assert probe_ids == expected_probes
    for check in report.checks:
        if check.category == "probe":
            assert check.status == "pass", f"{check.id} failed: {check.summary} {check.detail}"


@pytest.mark.skipif(sys.platform != "darwin", reason="CLI probe test on macOS")
def test_doctor_cli_json_output_and_exit_code(tmp_path: Path, capsys) -> None:
    exit_code = doctor_main(["--cwd", str(tmp_path), "--json", "--no-probe"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["platform"] == "darwin"
    assert isinstance(payload["checks"], list)
    assert exit_code in (0, 1)
