/**
 * Replaces shell/src/history.ts in the renderer.
 *
 * Prompt history is a convenience, not state worth an IPC round trip, so it
 * lives in localStorage rather than ~/.agent-smith/history.
 */
export const HISTORY_LIMIT = 200;

const KEY = "smith.history";

export function loadHistory(): string[] {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item): item is string => typeof item === "string").slice(-HISTORY_LIMIT);
  } catch {
    return [];
  }
}

export function saveHistory(history: string[]): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(history.slice(-HISTORY_LIMIT)));
  } catch {
    // A full or disabled localStorage must not take down the session.
  }
}
