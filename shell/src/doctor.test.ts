import assert from "node:assert/strict";
import test from "node:test";

import { formatDoctorReport, parseDoctorArgs } from "./doctor.js";

test("parseDoctorArgs parses --json, --no-probe, --cwd and rejects unknown flags", () => {
  const parsed = parseDoctorArgs(["--json", "--no-probe", "--cwd", "/tmp/work"], "/default");
  assert.ok(!("error" in parsed));
  assert.equal(parsed.asJson, true);
  assert.equal(parsed.noProbe, true);
  assert.ok(parsed.cwd.endsWith("/tmp/work"));

  const bad = parseDoctorArgs(["--bogus"], "/default");
  assert.ok("error" in bad);
  assert.match(bad.error, /Unknown option/);

  const missingCwd = parseDoctorArgs(["--cwd"], "/default");
  assert.ok("error" in missingCwd);
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
