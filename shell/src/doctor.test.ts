import assert from "node:assert/strict";
import test from "node:test";

import { buildPythonDoctorInvocation, formatDoctorReport, parseDoctorArgs } from "./doctor.js";

test("parseDoctorArgs parses --json, --no-probe, --cwd, --check-llm and rejects unknown flags", () => {
  const parsed = parseDoctorArgs(["--json", "--no-probe", "--check-llm", "--cwd", "/tmp/work"], "/default");
  assert.ok(!("error" in parsed));
  assert.equal(parsed.asJson, true);
  assert.equal(parsed.noProbe, true);
  assert.equal(parsed.checkLlm, true);
  assert.ok(parsed.cwd.endsWith("/tmp/work"));

  const bad = parseDoctorArgs(["--bogus"], "/default");
  assert.ok("error" in bad);
  assert.match(bad.error, /Unknown option/);

  const missingCwd = parseDoctorArgs(["--cwd"], "/default");
  assert.ok("error" in missingCwd);
});

test("buildPythonDoctorInvocation uses repoRoot and PYTHONPATH when uv is unavailable and forwards --check-llm", () => {
  const inv = buildPythonDoctorInvocation(
    "/repo",
    "/repo/engine",
    "/repo/server",
    { asJson: true, noProbe: true, cwd: "/work", checkLlm: true },
    false,
  );
  assert.equal(inv.cmd, "python3");
  assert.equal(inv.cwd, "/repo");
  assert.ok(inv.env.PYTHONPATH?.includes("/repo"));
  assert.ok(inv.args.includes("--check-llm"));
  assert.ok(inv.args.includes("--no-probe"));
});

test("formatDoctorReport renders PASS/WARN/FAIL badges and remediation blocks", () => {
  const text = formatDoctorReport({
    ok: false,
    platform: "linux",
    cwd: "/workspace",
    sandbox_backend: "unavailable",
    fallback_policy: "deny",
    duration_ms: 42,
    checks: [
      { id: "runtime.node", category: "runtime", status: "pass", summary: "Node.js v22.0.0" },
      {
        id: "sandbox.bwrap.ready",
        category: "sandbox",
        status: "fail",
        summary: "AppArmor blocked userns",
        code: "LINUX_APPARMOR_USERNS_BLOCKED",
        remediation: "sudo systemctl reload apparmor",
      },
    ],
  });

  assert.match(text, /\[PASS\] runtime\.node/);
  assert.match(text, /\[FAIL\] sandbox\.bwrap\.ready.*LINUX_APPARMOR_USERNS_BLOCKED/);
  assert.match(text, /sudo systemctl reload apparmor/);
  assert.match(text, /Summary: FAILED/);
});
