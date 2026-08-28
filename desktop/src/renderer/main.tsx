// Must run before anything from shell/src is evaluated — those modules read
// process.cwd() at call time, but the shim has to exist first.
import "./process-polyfill.ts";

import { createRoot } from "react-dom/client";
import { NodeBridge } from "../../../shell/src/bridge.js";
import { createAppStore } from "../../../shell/src/store.js";
import { App } from "./App.tsx";
import { setWorkingDir } from "./process-polyfill.ts";
import { loadHistory } from "./shims/history.ts";
import "./styles.css";

setWorkingDir(await window.smith.workingDir());

// The same three lines shell/src/index.tsx uses to start up.
const store = createAppStore(loadHistory());
const bridge = new NodeBridge(store);

const root = document.getElementById("root");
if (!root) throw new Error("#root missing");
createRoot(root).render(<App store={store} bridge={bridge} />);

if (import.meta.env.DEV) {
  // Dev-only handle so the renderer can be driven over CDP without a live model.
  (window as unknown as { __smith?: unknown }).__smith = { store, bridge };
}

void bridge.boot();
