/**
 * Minimal POSIX path for the renderer. Electron renderers are macOS/Linux/Windows
 * but every path here is compared against a POSIX project root that the main
 * process resolved, so POSIX semantics are the correct ones.
 */
export const sep = "/";

export function resolve(...segments: string[]): string {
  let out = "";
  for (const segment of segments) {
    if (!segment) continue;
    out = segment.startsWith("/") ? segment : out ? `${out}/${segment}` : segment;
  }
  if (!out.startsWith("/")) out = `/${out}`;

  const parts: string[] = [];
  for (const part of out.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") parts.pop();
    else parts.push(part);
  }
  return `/${parts.join("/")}`;
}

export function extname(value: string): string {
  const base = value.slice(value.lastIndexOf("/") + 1);
  const dot = base.lastIndexOf(".");
  return dot <= 0 ? "" : base.slice(dot);
}

export function basename(value: string): string {
  return value.slice(value.lastIndexOf("/") + 1);
}

export function dirname(value: string): string {
  const cut = value.lastIndexOf("/");
  if (cut < 0) return ".";
  return cut === 0 ? "/" : value.slice(0, cut);
}

export function join(...segments: string[]): string {
  return resolve(...segments);
}

export default { sep, resolve, extname, basename, dirname, join };
