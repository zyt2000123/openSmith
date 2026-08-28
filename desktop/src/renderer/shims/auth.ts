/**
 * Replaces shell/src/auth.ts in the renderer.
 *
 * The token lives at ~/.agent-smith/auth_token; the renderer has no fs, and
 * handing it the raw token would put a credential in a Chromium heap that any
 * XSS could read. The main process reads and caches it, and returns only the
 * finished header.
 */
export function buildAuthHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` };
}

export async function localAuthHeaders(): Promise<Record<string, string>> {
  return window.smith.authHeaders();
}
