/** Persistent input history — a JSON string array at ~/.agent-smith/shell_history.json. */

import { randomUUID } from "node:crypto";
import { closeSync, mkdirSync, openSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

export const HISTORY_LIMIT = 200;

// Credential shapes copied from the trace store's redaction so a prompt that
// pasted an API key/token is not persisted verbatim to disk.
const SECRET_PATTERN =
  /(?<![A-Za-z0-9])(?:\b(?:bearer|basic)\s+(?=[A-Za-z0-9._~+/=-]*[0-9])[A-Za-z0-9._~+/=-]{16,}|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{30,}|AKIA[0-9A-Z]{12,}|xox[abprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+)/gi;

function redactHistory(text: string): string {
  return text.replace(SECRET_PATTERN, "[REDACTED]");
}

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
  // Unpredictable suffix + O_EXCL: a same-user process cannot pre-plant a
  // symlink at the temp path to redirect the write (TOCTOU).
  const temporary = path.join(path.dirname(file), `.${path.basename(file)}.${process.pid}.${randomUUID()}.tmp`);
  let fd: number | null = null;
  try {
    mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
    fd = openSync(temporary, "wx", 0o600);
    writeFileSync(fd, JSON.stringify(history.slice(-HISTORY_LIMIT).map(redactHistory)), { encoding: "utf8" });
    closeSync(fd);
    fd = null;
    renameSync(temporary, file);
  } catch {
    try {
      if (fd !== null) closeSync(fd);
    } catch {
      // ignore
    }
    try {
      rmSync(temporary, { force: true });
    } catch {
      // ignore cleanup failures too — history is best-effort
    }
    // ignore — history is best-effort
  }
}
