#!/usr/bin/env node
// Capture the directory from which the global CLI was invoked before any
// runtime/bootstrap code can change the process working directory.
process.env.SMITH_PROJECT_CWD ??= process.cwd();

if (process.argv[2] === "doctor") {
  const { runDoctorCli } = await import("../dist/doctor.js");
  const exitCode = await runDoctorCli(process.argv.slice(3));
  process.exit(exitCode);
}

await import("../dist/index.js");
