/**
 * Stub for node:fs in the renderer, needed only by shell/src/smith-ui-schema.ts.
 *
 * Its fs calls are a security boundary, not a convenience: realpathSync/statSync
 * reject path traversal out of the project root, and readFileSync backs the
 * decode-bomb check. None of that can run in a renderer with no filesystem, so
 * these throw — and the caller wraps them in `try { … } catch { return null }`,
 * which rejects the image. Failing closed is the point; a stub that returned
 * plausible values would silently disable both checks.
 *
 * Images come back once the check runs over IPC in the main process.
 */
const unavailable = () => {
  throw new Error("node:fs is unavailable in the renderer");
};

export const readFileSync = unavailable as (...args: unknown[]) => Buffer;
export const realpathSync = unavailable as (...args: unknown[]) => string;
export const statSync = unavailable as (...args: unknown[]) => never;

export default { readFileSync, realpathSync, statSync };
