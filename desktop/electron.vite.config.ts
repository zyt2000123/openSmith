import { existsSync } from "node:fs";
import { basename, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "electron-vite";
import type { Plugin } from "vite";

const here = dirname(fileURLToPath(import.meta.url));
const SHELL_SRC = resolve(here, "../shell/src");
const SHIMS = resolve(here, "src/renderer/shims");

/**
 * Lets desktop reuse `shell/src` verbatim — shell is stable and must not change.
 *
 * Two jobs, both resolve-time only:
 *  1. shell uses ESM-style `./foo.js` specifiers that point at `foo.ts` sources.
 *     Vite does not do that mapping on its own.
 *  2. Five shell modules reach for Node or the TTY. In the renderer they are
 *     swapped for IPC/DOM equivalents. `text-layout` is pure JS and stays.
 */
function shellBridge(swapForRenderer: boolean): Plugin {
  const swap: Record<string, string> = swapForRenderer
    ? {
        term: `${SHIMS}/term.ts`,
        "dev-server": `${SHIMS}/dev-server.ts`,
        auth: `${SHIMS}/auth.ts`,
        history: `${SHIMS}/history.ts`,
      }
    : {};

  return {
    name: "shell-bridge",
    enforce: "pre",
    resolveId(source, importer) {
      if (!importer || !source.startsWith(".")) return null;

      // Keyed on the *target*, not the importer: desktop's own files import
      // shell/src directly, and those specifiers need the same treatment.
      const target = resolve(dirname(importer), source);
      if (!target.startsWith(SHELL_SRC)) return null;
      const stem = target.replace(/\.js$/, "");

      // Only shell/src's own top-level modules are swappable.
      if (dirname(stem) === SHELL_SRC) {
        const shim = swap[basename(stem)];
        if (shim) return shim;
      }

      for (const ext of [".ts", ".tsx"]) {
        if (existsSync(stem + ext)) return stem + ext;
      }
      return null;
    },
  };
}

export default defineConfig({
  main: {
    // The main process is Node, so shell's dev-server.ts and auth.ts run as-is.
    plugins: [shellBridge(false)],
    build: {
      rollupOptions: { input: resolve(here, "src/main/index.ts") },
    },
  },
  preload: {
    build: {
      rollupOptions: {
        input: resolve(here, "src/preload/index.ts"),
        // A sandboxed preload must be CommonJS — Electron does not load an ESM
        // preload under sandbox:true. package.json is type:module, so the .cjs
        // extension is what stops Node reading this back as ESM.
        output: { format: "cjs", entryFileNames: "index.cjs" },
      },
    },
  },
  renderer: {
    root: resolve(here, "src/renderer"),
    plugins: [shellBridge(true), react()],
    resolve: {
      alias: {
        // smith-ui-schema reads image bytes with node:fs; only the dimension
        // probe needs it, so a stub keeps its 368 lines of parsing intact.
        "node:fs": `${SHIMS}/node-fs.ts`,
        "node:path": `${SHIMS}/node-path.ts`,
      },
    },
    build: {
      rollupOptions: { input: resolve(here, "src/renderer/index.html") },
    },
  },
});
