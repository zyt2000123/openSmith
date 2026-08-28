import type { SmithApi } from "../preload/index.js";

declare global {
  interface Window {
    smith: SmithApi;
  }
}
