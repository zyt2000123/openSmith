import { readFile, stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const AUTH_TOKEN_PATH = path.join(os.homedir(), ".agent-smith", "auth_token");

let permissionChecked = false;

export function buildAuthHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` };
}

export async function localAuthHeaders(): Promise<Record<string, string>> {
  let token: string;
  try {
    token = (await readFile(AUTH_TOKEN_PATH, "utf8")).trim();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    // The local server writes this file on first start, so name it: a shell pointed at a
    // remote SMITH_SERVER_URL has no other clue why every request fails.
    throw new Error(`Local Smith auth token is unavailable at ${AUTH_TOKEN_PATH}: ${message}`);
  }

  if (!token) throw new Error("Local Smith auth token is empty.");

  // The token authenticates this machine's API.  If it is group/world readable,
  // warn once (per process) so a loose umask does not silently leak it to other
  // local users without spamming stderr on every request.
  if (!permissionChecked) {
    try {
      const info = await stat(AUTH_TOKEN_PATH);
      // The check is done for this process — mark it before warning so a
      // correctly-permissioned (mode 600) token is not re-stat'd on every
      // request, which the old warn-only flag left happening forever.
      permissionChecked = true;
      if (info.mode & 0o077) {
        console.warn(
          `Warning: ${AUTH_TOKEN_PATH} is readable by other users (mode 0${(info.mode & 0o777).toString(8)}); ` +
            "chmod 600 it to keep the local API token private.",
        );
      }
    } catch {
      // Permission problems surface at the read above; do not fail auth over a
      // stat, and leave the flag unset so a transient stat error retries.
    }
  }

  return buildAuthHeaders(token);
}
