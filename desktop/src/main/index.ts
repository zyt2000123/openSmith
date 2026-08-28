import { app, BrowserWindow, ipcMain, shell as electronShell } from "electron";
import { existsSync } from "node:fs";
import { join, resolve } from "node:path";
import { localAuthHeaders } from "../../../shell/src/auth.js";
import { ensureLocalServer, stopOwnedServer } from "../../../shell/src/dev-server.js";

// The main process is Node, so shell's originals run unchanged — only the
// renderer needs the shims. shell/ is stable and stays untouched.

// dev-server.ts locates the checkout from its own bundle path, which lands in
// desktop/ rather than the repo root, and then from cwd — also desktop/. Its
// documented override is SMITH_REPO_ROOT, read at call time, so setting it here
// is enough and shell stays untouched.
const repoRoot = resolve(__dirname, "../../..");
if (!process.env.SMITH_REPO_ROOT?.trim() && existsSync(join(repoRoot, "server"))) {
  process.env.SMITH_REPO_ROOT = repoRoot;
}

// Dev-only: lets the renderer be inspected over CDP without a screen capture.
if (!app.isPackaged) app.commandLine.appendSwitch("remote-debugging-port", "9333");

let serverPromise: ReturnType<typeof ensureLocalServer> | null = null;

function connectServer() {
  // One boot per app run: several renderer windows must not each spawn uvicorn.
  serverPromise ??= ensureLocalServer();
  return serverPromise;
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    show: false,
    title: "Smith",
    backgroundColor: "#0d0d0d",
    ...(process.platform === "darwin"
      ? { titleBarStyle: "hidden" as const, trafficLightPosition: { x: 14, y: 14 } }
      : { frame: false, titleBarStyle: "hidden" as const }),
    webPreferences: {
      preload: join(__dirname, "../preload/index.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  win.once("ready-to-show", () => win.show());

  // Anything not our own page opens in the user's browser, never in-app.
  win.webContents.setWindowOpenHandler(({ url }) => {
    void electronShell.openExternal(url);
    return { action: "deny" };
  });

  if (process.env.ELECTRON_RENDERER_URL) {
    void win.loadURL(process.env.ELECTRON_RENDERER_URL);
  } else {
    void win.loadFile(join(__dirname, "../renderer/index.html"));
  }
  return win;
}

ipcMain.handle("smith:ensure-server", () => connectServer());
ipcMain.handle("smith:stop-server", () => stopOwnedServer());
ipcMain.handle("smith:auth-headers", () => localAuthHeaders());
// Electron's cwd is desktop/, so process.cwd() would point Smith at the wrong
// tree — every relative path a tool resolves would land one level too deep.
ipcMain.handle(
  "smith:working-dir",
  () => process.env.SMITH_PROJECT_CWD?.trim() || process.env.SMITH_REPO_ROOT || process.cwd(),
);

app.whenReady().then(() => {
  // Start the backend and the window together; the renderer awaits the server
  // through bridge.boot() anyway, so serialising them only adds latency.
  void connectServer().catch(() => {
    // Surfaced in the renderer via bridge.boot(); a rejected promise here would
    // otherwise be an unhandled rejection that kills nothing useful.
  });
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  void stopOwnedServer();
});
