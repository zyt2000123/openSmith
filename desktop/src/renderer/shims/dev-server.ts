/**
 * Replaces shell/src/dev-server.ts in the renderer.
 *
 * The real module spawns uvicorn and probes health — Node-only work that stays
 * in the main process, which imports shell's original file unchanged.
 */
export type ServerConnection = {
  baseUrl: string;
  started: boolean;
  note?: string;
};

export async function ensureLocalServer(): Promise<ServerConnection> {
  return window.smith.ensureServer();
}

export function stopOwnedServer(): Promise<void> {
  return window.smith.stopServer();
}
