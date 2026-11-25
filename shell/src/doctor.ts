import { spawnSync } from "node:child_process";
import { existsSync, statSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import type { DoctorCheck, DoctorReport } from "./api.js";
import { resolveRepoRoot } from "./dev-server.js";

export type DoctorCliOptions = {
  asJson: boolean;
  noProbe: boolean;
  cwd: string;
  checkLlm: boolean;
};

export function parseDoctorArgs(argv: string[], defaultCwd: string): DoctorCliOptions | { error: string } {
  let asJson = false;
  let noProbe = false;
  let checkLlm = false;
  let cwd = defaultCwd;

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--json") {
      asJson = true;
    } else if (arg === "--no-probe") {
      noProbe = true;
    } else if (arg === "--check-llm") {
      checkLlm = true;
    } else if (arg === "--cwd") {
      const next = argv[i + 1];
      if (!next || next.startsWith("-")) {
        return { error: "--cwd requires a directory path" };
      }
      cwd = path.resolve(next);
      i += 1;
    } else if (arg?.startsWith("--cwd=")) {
      const value = arg.slice("--cwd=".length);
      if (!value) return { error: "--cwd requires a directory path" };
      cwd = path.resolve(value);
    } else if (arg === "--help" || arg === "-h") {
      return {
        error: "Usage: smith doctor [--json] [--no-probe] [--cwd <dir>] [--check-llm]",
      };
    } else {
      return { error: `Unknown option: ${arg}` };
    }
  }

  return { asJson, noProbe, cwd, checkLlm };
}

function checkNodeRuntime(): DoctorCheck {
  const major = Number.parseInt(process.versions.node.split(".")[0] ?? "0", 10);
  if (Number.isNaN(major) || major < 22) {
    return {
      id: "runtime.node",
      category: "runtime",
      status: "fail",
      summary: `Node.js ${process.versions.node} is below the required >=22`,
      code: "NODE_VERSION_UNSUPPORTED",
      remediation: "Upgrade Node.js to >=22.",
    };
  }
  return {
    id: "runtime.node",
    category: "runtime",
    status: "pass",
    summary: `Node.js v${process.versions.node}`,
  };
}

function checkUvBinary(): { check: DoctorCheck; available: boolean } {
  const res = spawnSync("uv", ["--version"], { encoding: "utf8", timeout: 5_000 });
  if (res.error || res.status !== 0) {
    return {
      available: false,
      check: {
        id: "runtime.uv",
        category: "runtime",
        status: "fail",
        summary: "uv is not installed or not in PATH",
        code: "UV_NOT_INSTALLED",
        remediation: "Install uv (https://docs.astral.sh/uv/getting-started/installation/).",
      },
    };
  }
  const version = (res.stdout || res.stderr).trim().split("\n")[0] || "uv";
  return {
    available: true,
    check: {
      id: "runtime.uv",
      category: "runtime",
      status: "pass",
      summary: version,
    },
  };
}

function checkServerLayout(repoRoot: string): { check: DoctorCheck; serverDir: string; engineDir: string } {
  const serverDir = path.join(repoRoot, "server");
  const engineDir = path.join(repoRoot, "engine");
  const hasServer = existsSync(path.join(serverDir, "app", "main.py"));
  const hasEngine = existsSync(path.join(engineDir, "sandbox", "doctor.py"));
  if (!hasServer || !hasEngine) {
    return {
      serverDir,
      engineDir,
      check: {
        id: "runtime.repo_layout",
        category: "runtime",
        status: "fail",
        summary: `Agent-Smith server/engine checkout not found under ${repoRoot}`,
        code: "REPO_LAYOUT_MISSING",
        remediation: "Set SMITH_REPO_ROOT to your Agent-Smith checkout directory.",
      },
    };
  }
  return {
    serverDir,
    engineDir,
    check: {
      id: "runtime.repo_layout",
      category: "runtime",
      status: "pass",
      summary: `Repository root resolved (${repoRoot})`,
    },
  };
}

function checkLocalDataPermissions(): DoctorCheck[] {
  const dataDir = path.join(os.homedir(), ".agent-smith");
  if (!existsSync(dataDir)) return [];
  const checks: DoctorCheck[] = [];
  try {
    const mode = statSync(dataDir).mode & 0o777;
    if ((mode & 0o077) !== 0) {
      checks.push({
        id: "shell.permissions.data_dir",
        category: "permissions",
        status: "fail",
        summary: `${dataDir} permissions are too open (0${mode.toString(8)})`,
        code: "DATA_DIR_PERMISSIONS_LOOSE",
        remediation: `chmod 700 ${dataDir}`,
      });
    }
  } catch {
    // Engine-side check handles unreadable directory details.
  }
  return checks;
}

export function buildPythonDoctorInvocation(
  repoRoot: string,
  engineDir: string,
  serverDir: string,
  options: DoctorCliOptions,
  uvAvailable: boolean,
): { cmd: string; args: string[]; cwd: string; env: NodeJS.ProcessEnv } {
  const pythonArgs = ["-m", "engine.sandbox.doctor", "--cwd", options.cwd, "--json"];
  if (options.noProbe) pythonArgs.push("--no-probe");
  if (options.checkLlm) pythonArgs.push("--check-llm");

  if (uvAvailable) {
    const projectDir = existsSync(path.join(serverDir, ".venv")) ? serverDir : engineDir;
    return {
      cmd: "uv",
      args: ["run", "--project", projectDir, "python", ...pythonArgs],
      cwd: projectDir,
      env: { ...process.env },
    };
  }
  // L5: Without uv, run python3 from repoRoot with PYTHONPATH=repoRoot so
  // `engine.sandbox.doctor` and `common.*` resolve cleanly instead of failing
  // with ModuleNotFoundError inside engine/ or server/.
  const existingPyPath = process.env.PYTHONPATH;
  const pythonPath = existingPyPath ? `${repoRoot}${path.delimiter}${existingPyPath}` : repoRoot;
  return {
    cmd: "python3",
    args: pythonArgs,
    cwd: repoRoot,
    env: { ...process.env, PYTHONPATH: pythonPath },
  };
}

function runPythonDoctor(
  repoRoot: string,
  engineDir: string,
  serverDir: string,
  options: DoctorCliOptions,
  uvAvailable: boolean,
): { report: DoctorReport | null; errorCheck?: DoctorCheck } {
  const invocation = buildPythonDoctorInvocation(repoRoot, engineDir, serverDir, options, uvAvailable);

  const res = spawnSync(invocation.cmd, invocation.args, {
    cwd: invocation.cwd,
    encoding: "utf8",
    timeout: 60_000,
    env: invocation.env,
  });

  if (res.error) {
    return {
      report: null,
      errorCheck: {
        id: "engine.doctor",
        category: "runtime",
        status: "fail",
        summary: `Failed to execute engine doctor (${invocation.cmd}): ${res.error.message}`,
        code: "ENGINE_DOCTOR_EXEC_FAILED",
      },
    };
  }

  const rawOut = (res.stdout || "").trim();
  try {
    const parsed = JSON.parse(rawOut) as DoctorReport;
    if (parsed && Array.isArray(parsed.checks)) {
      return { report: parsed };
    }
  } catch {
    // Fall through to structured error check below.
  }

  return {
    report: null,
    errorCheck: {
      id: "engine.doctor",
      category: "runtime",
      status: "fail",
      summary: `Engine doctor returned invalid JSON (exit ${res.status ?? "unknown"})`,
      detail: (res.stderr || rawOut).slice(0, 400),
      code: "ENGINE_DOCTOR_INVALID_OUTPUT",
    },
  };
}

export function formatDoctorReport(report: DoctorReport): string {
  const badges: Record<DoctorCheck["status"], string> = {
    pass: "[PASS]",
    warn: "[WARN]",
    fail: "[FAIL]",
    skip: "[SKIP]",
  };
  const lines = [
    `Agent-Smith Doctor (${report.platform} · sandbox=${report.sandbox_backend} · ${report.duration_ms}ms)`,
    `Workspace: ${report.cwd}`,
    "",
  ];
  for (const check of report.checks) {
    const code = check.code ? ` (${check.code})` : "";
    lines.push(`  ${badges[check.status]} ${check.id}: ${check.summary}${code}`);
    if (check.detail) {
      lines.push(`         detail: ${check.detail}`);
    }
    if (check.remediation) {
      lines.push("         fix:");
      for (const line of check.remediation.split("\n")) {
        lines.push(`           ${line}`);
      }
    }
  }
  const passed = report.checks.filter((c) => c.status === "pass").length;
  const warned = report.checks.filter((c) => c.status === "warn").length;
  const failed = report.checks.filter((c) => c.status === "fail").length;
  lines.push("");
  lines.push(`Summary: ${report.ok ? "OK" : "FAILED"} — ${passed} passed, ${warned} warning(s), ${failed} failed`);
  return lines.join("\n");
}

export async function runDoctorCli(argv: string[]): Promise<number> {
  const started = Date.now();
  const defaultCwd = process.env.SMITH_PROJECT_CWD || process.cwd();
  const parsed = parseDoctorArgs(argv, defaultCwd);
  if ("error" in parsed) {
    process.stderr.write(`${parsed.error}\n`);
    return 2;
  }

  const nodeChecks: DoctorCheck[] = [checkNodeRuntime()];
  const uv = checkUvBinary();
  nodeChecks.push(uv.check);
  const repoRoot = resolveRepoRoot();
  const layout = checkServerLayout(repoRoot);
  nodeChecks.push(layout.check);
  nodeChecks.push(...checkLocalDataPermissions());

  const engineResult =
    layout.check.status === "pass"
      ? runPythonDoctor(repoRoot, layout.engineDir, layout.serverDir, parsed, uv.available)
      : { report: null };

  const checks: DoctorCheck[] = [...nodeChecks];
  if (engineResult.report) {
    checks.push(...engineResult.report.checks);
  } else if (engineResult.errorCheck) {
    checks.push(engineResult.errorCheck);
  }

  const combined: DoctorReport = {
    ok: !checks.some((c) => c.status === "fail"),
    platform: engineResult.report?.platform ?? process.platform,
    cwd: engineResult.report?.cwd ?? parsed.cwd,
    sandbox_backend: engineResult.report?.sandbox_backend ?? "unavailable",
    fallback_policy: engineResult.report?.fallback_policy ?? "deny",
    checks,
    duration_ms: Date.now() - started,
  };

  if (parsed.asJson) {
    process.stdout.write(`${JSON.stringify(combined, null, 2)}\n`);
  } else {
    process.stdout.write(`${formatDoctorReport(combined)}\n`);
  }
  return combined.ok ? 0 : 1;
}
