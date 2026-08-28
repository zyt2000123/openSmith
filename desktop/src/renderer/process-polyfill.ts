/**
 * shell/src reads process.cwd() and process.env.SMITH_PROJECT_CWD in two places
 * (bridge.ts sends the working dir with each message; commands.ts uses it for
 * project init). A sandboxed renderer has no `process`, and shell must not
 * change, so supply the two members it actually touches.
 *
 * cwd is filled in from IPC during boot, before any message can be sent.
 */
let workingDir = "/";

export function setWorkingDir(value: string): void {
  workingDir = value;
}

const shim = {
  env: {} as Record<string, string | undefined>,
  cwd: () => workingDir,
  platform: "browser",
};

// biome-ignore lint/suspicious/noExplicitAny: deliberate global shim
(globalThis as any).process ??= shim;
