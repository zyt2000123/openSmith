/** Persistent input history — a JSON string array at ~/.agent-smith/shell_history.json. */

import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

export const HISTORY_LIMIT = 200;

function historyPath(): string {
  return path.join(homedir(), ".agent-smith", "shell_history.json");
}

export function loadHistory(file = historyPath()): string[] {
  try {
    const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item): item is string => typeof item === "string").slice(-HISTORY_LIMIT);
  } catch {
    return [];
  }
}

/** Atomically persist before the process can exit; history failures never break input handling. */
export function saveHistory(history: string[], file = historyPath()): void {
  const temporary = path.join(path.dirname(file), `.${path.basename(file)}.${process.pid}.tmp`);
  try {
    mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
    writeFileSync(temporary, JSON.stringify(history.slice(-HISTORY_LIMIT)), { encoding: "utf8", mode: 0o600 });
    renameSync(temporary, file);
  } catch {
    try {
      rmSync(temporary, { force: true });
    } catch {
      // ignore cleanup failures too — history is best-effort
    }
    // ignore — history is best-effort
  }
}
