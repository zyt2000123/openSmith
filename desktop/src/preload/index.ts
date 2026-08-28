import { contextBridge, ipcRenderer } from "electron";

/**
 * The renderer gets finished results, never capabilities: a header string
 * rather than the token, a base URL rather than a spawn handle.
 */
const smith = {
  ensureServer: (): Promise<{ baseUrl: string; started: boolean; note?: string }> =>
    ipcRenderer.invoke("smith:ensure-server"),
  stopServer: (): Promise<void> => ipcRenderer.invoke("smith:stop-server"),
  authHeaders: (): Promise<Record<string, string>> => ipcRenderer.invoke("smith:auth-headers"),
  workingDir: (): Promise<string> => ipcRenderer.invoke("smith:working-dir"),
};

contextBridge.exposeInMainWorld("smith", smith);

export type SmithApi = typeof smith;
