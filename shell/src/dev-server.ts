import { type ChildProcess, spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { createServer } from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { createTimeoutSignal } from "./api.js";

const DEFAULT_SERVER_URL = "http://127.0.0.1:8140";
const SERVER_PROBE_TIMEOUT_MS = 3_000;
const SERVER_STARTUP_TIMEOUT_MS = 30_000;
export const REQUIRED_API_OPERATIONS = [
  { method: "GET", path: "/api/config/llm" },
  { method: "GET", path: "/api/config/llm/models" },
  { method: "POST", path: "/api/config/llm" },
  { method: "GET", path: "/api/agent" },
  { method: "POST", path: "/api/agent/ensure" },
  { method: "PUT", path: "/api/agent/project-instructions" },
  { method: "GET", path: "/api/agent/sessions" },
  { method: "POST", path: "/api/agent/sessions" },
  { method: "GET", path: "/api/agent/sessions/{session_id}/messages" },
  { method: "POST", path: "/api/agent/sessions/{session_id}/messages/stream" },
  { method: "PATCH", path: "/api/agent/sessions/{session_id}/model" },
  { method: "POST", path: "/api/agent/sessions/{session_id}/compress" },
  { method: "DELETE", path: "/api/agent/sessions/{session_id}" },
  { method: "GET", path: "/api/agent/skills" },
  { method: "PUT", path: "/api/agent/skills/{skill_name}" },
  { method: "GET", path: "/api/agent/mcp" },
  { method: "GET", path: "/api/agent/memory/status" },
  { method: "GET", path: "/api/agent/token-stats" },
  { method: "GET", path: "/api/agent/observability/runs" },
  { method: "GET", path: "/api/agent/observability/health" },
  { method: "GET", path: "/api/agent/observability/incidents" },
  { method: "GET", path: "/api/agent/observability/runs/{run_id}/diagnosis" },
  { method: "GET", path: "/api/agent/observability/runs/{run_id}/improvement-proposal" },
  { method: "GET", path: "/api/agent/runs/{run_id}" },
  { method: "POST", path: "/api/agent/runs/{run_id}/resume" },
  { method: "POST", path: "/api/agent/runs/{run_id}/approval" },
] as const;

type OpenApiPathItem = Record<string, unknown>;

export function findMissingApiOperations(paths: Record<string, unknown>): string[] {
  return REQUIRED_API_OPERATIONS.flatMap(({ method, path }) => {
    const item = paths[path];
    const operation =
      item && typeof item === "object" && !Array.isArray(item)
        ? (item as OpenApiPathItem)[method.toLowerCase()]
        : undefined;
    return operation && typeof operation === "object" ? [] : [`${method} ${path}`];
  });
}

type ServerConnection = {
  baseUrl: string;
  started: boolean;
  note?: string;
};

type ServerTarget = {
  baseUrl: string;
  envOverride: boolean;
  preferredPort: number;
};

const SERVER_TERMINATION_GRACE_MS = 1_000;
// If SIGTERM (and the follow-up SIGKILL) still cannot reap the child, give up
// rather than hanging the shell in raw mode forever.
const SERVER_TERMINATION_HARD_TIMEOUT_MS = 5_000;
const STDERR_KEEP_CHUNKS = 40;
const STDERR_TAIL_LINES = 8;
const STDERR_TAIL_CHARS = 800;

/** Last few stderr lines, bounded, for a startup-failure message. */
function stderrTail(chunks: string[]): string {
  const text = chunks.join("").trimEnd();
  if (!text) return "";
  const tail = text.split("\n").slice(-STDERR_TAIL_LINES).join("\n");
  return tail.length > STDERR_TAIL_CHARS ? tail.slice(-STDERR_TAIL_CHARS) : tail;
}

const LOOPBACK_HOST_RE = /^(localhost|127\.\d+\.\d+\.\d+|\[?::1\]?)$/;

/** Refuse a SMITH_SERVER_URL that would exfiltrate the local auth token.
 *
 * Every request carries `Authorization: Bearer <~/.agent-smith/auth_token>` and
 * setup can POST the user's LLM API key.  Sending those in cleartext to a
 * non-loopback host is never acceptable; an explicit https:// remote is the
 * user's deployment choice, but a plaintext http:// one is refused outright.
 */
function assertSafeServerTarget(target: ServerTarget): void {
  if (!target.envOverride) return;
  let parsed: URL;
  try {
    parsed = new URL(target.baseUrl);
  } catch {
    throw new Error(`SMITH_SERVER_URL is not a valid URL: ${target.baseUrl}`);
  }
  const host = parsed.hostname
    .toLowerCase()
    .replace(/^\[|\]$/g, "")
    .replace(/\.$/, "");
  if (parsed.protocol === "http:" && !LOOPBACK_HOST_RE.test(host)) {
    throw new Error(
      `SMITH_SERVER_URL=${target.baseUrl} would send the local auth token over cleartext ` +
        "HTTP to a remote host; set it to a loopback address or use https://.",
    );
  }
}

type LaunchedServer = {
  child: ChildProcess;
  nonce: string;
  getSpawnError: () => Error | undefined;
  getStderrTail: () => string;
};

let ownedServer: ChildProcess | null = null;
let cleanupRegistered = false;
let stopPromise: Promise<void> | null = null;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

/** Send a signal to the child's whole process group.
 *
 * `uv run uvicorn ...` spawns uvicorn as a grandchild; signalling only the `uv`
 * wrapper lets the port-holding uvicorn survive as an orphan.  The child is
 * spawned detached so it leads its own process group and a negative pid reaches
 * every descendant.
 */
function signalProcessGroup(child: ChildProcess, signal: NodeJS.Signals): boolean {
  if (!child.pid) return false;
  try {
    process.kill(-child.pid, signal);
    return true;
  } catch {
    try {
      return child.kill(signal);
    } catch {
      return false;
    }
  }
}

function cleanupOwnedServer(): void {
  const child = ownedServer;
  ownedServer = null;
  if (!child || child.exitCode !== null) return;

  // Last-resort reap for paths that cannot await: the process 'exit' event.  A
  // setTimeout scheduled here never fires — the event loop is already tearing
  // down — so the previous SIGTERM-then-timer-SIGKILL escalation delivered only
  // SIGTERM and left a wedged uvicorn orphaned holding the port and auth token.
  // Normal exits use the awaitable stopOwnedServer() with a real grace period;
  // this path is only reached on a crash/uncaught exception, where the child has
  // no other client, so go straight to SIGKILL to guarantee the reap.
  signalProcessGroup(child, "SIGKILL");
}

/** Stop the owned server and await its exit.  Safe to call multiple times. */
export function stopOwnedServer(): Promise<void> {
  if (stopPromise === null) {
    stopPromise = performStopOwnedServer().finally(() => {
      stopPromise = null;
    });
  }
  return stopPromise;
}

async function performStopOwnedServer(): Promise<void> {
  const child = ownedServer;
  if (!child || child.exitCode !== null) return;
  // Do NOT clear ownedServer here: if the child survives the hard timeout, the
  // sync `exit`-event fallback (cleanupOwnedServer) must still be able to see
  // and signal it instead of leaving a port-holding orphan unreachable.
  const finished = new Promise<void>((resolve) => {
    const finish = (): void => {
      clearTimeout(escalation);
      clearTimeout(hard);
      // Only forget the child once it has actually exited: an 'error' event can
      // fire while the child still lives, and nulling early would leave the
      // sync exit-handler fallback unable to reap a still-alive orphan.
      if (child.exitCode !== null && ownedServer === child) ownedServer = null;
      resolve();
    };
    const escalation = setTimeout(() => {
      // Only escalate while the child is still alive; an unconditional SIGKILL
      // on the group could hit an unrelated process group whose leader PID was
      // reused after a prompt child exit.
      if (child.exitCode === null) signalProcessGroup(child, "SIGKILL");
    }, SERVER_TERMINATION_GRACE_MS);
    const hard = setTimeout(finish, SERVER_TERMINATION_HARD_TIMEOUT_MS);
    hard.unref?.();
    child.once("exit", finish);
    child.once("error", finish);
  });
  signalProcessGroup(child, "SIGTERM");
  await finished;
}

function registerCleanup(): void {
  if (cleanupRegistered) return;

  cleanupRegistered = true;
  process.once("exit", cleanupOwnedServer);
}

export function resolveRepoRoot(): string {
  const configuredRoot = process.env.SMITH_REPO_ROOT?.trim();
  if (configuredRoot) return path.resolve(configuredRoot);

  const distDir = path.dirname(fileURLToPath(import.meta.url));
  const packageRoot = path.resolve(distDir, "..", "..");
  if (existsSync(path.join(packageRoot, "server"))) return packageRoot;

  const currentRoot = path.resolve(process.cwd());
  return existsSync(path.join(currentRoot, "server")) ? currentRoot : packageRoot;
}

function serverTarget(): ServerTarget {
  const baseUrl = process.env.SMITH_SERVER_URL ?? DEFAULT_SERVER_URL;
  let parsedUrl: URL;
  try {
    parsedUrl = new URL(baseUrl);
  } catch {
    // A malformed SMITH_SERVER_URL is parsed here first, so name it: otherwise a
    // raw "Invalid URL" TypeError surfaced before assertSafeServerTarget's
    // crafted message could ever run.
    throw new Error(`SMITH_SERVER_URL is not a valid URL: ${baseUrl}`);
  }
  const fallbackPort = parsedUrl.protocol === "https:" ? "443" : "80";
  return {
    baseUrl,
    envOverride: Boolean(process.env.SMITH_SERVER_URL),
    preferredPort: Number.parseInt(parsedUrl.port || fallbackPort, 10),
  };
}

/** Build a server endpoint without losing a configured reverse-proxy path prefix. */
function serverEndpoint(baseUrl: string, pathname: string): string {
  return new URL(pathname.replace(/^\/+/, ""), `${baseUrl.replace(/\/+$/, "")}/`).toString();
}

async function isHealthy(baseUrl: string): Promise<boolean> {
  const timeout = createTimeoutSignal(SERVER_PROBE_TIMEOUT_MS);
  try {
    return (await fetch(serverEndpoint(baseUrl, "/api/health"), { signal: timeout.signal })).ok;
  } catch {
    return false;
  } finally {
    timeout.dispose();
  }
}

async function compatibilityIssue(baseUrl: string): Promise<string | null> {
  const timeout = createTimeoutSignal(SERVER_PROBE_TIMEOUT_MS);
  try {
    const response = await fetch(serverEndpoint(baseUrl, "/openapi.json"), { signal: timeout.signal });
    if (!response.ok) return `openapi responded with HTTP ${response.status}`;

    const payload = (await response.json()) as { paths?: Record<string, unknown> };
    const missingOperations = findMissingApiOperations(payload.paths ?? {});
    return missingOperations.length === 0 ? null : `missing API operations: ${missingOperations.join(", ")}`;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return `could not inspect OpenAPI schema: ${message}`;
  } finally {
    timeout.dispose();
  }
}

async function inspectExistingServer(
  target: ServerTarget,
): Promise<{ healthy: boolean; connection: ServerConnection | null; issue?: string }> {
  const healthy = await isHealthy(target.baseUrl);
  if (!healthy) return { healthy: false, connection: null };

  const issue = await compatibilityIssue(target.baseUrl);
  if (!issue) return { healthy: true, connection: { baseUrl: target.baseUrl, started: false } };
  if (target.envOverride) throw new Error(`Configured SMITH_SERVER_URL points to an incompatible server: ${issue}`);
  // The same reason was already computed here; the default path used to drop it
  // and start a second server with no explanation of why the first was rejected.
  return { healthy: true, connection: null, issue };
}

async function isCompatibleServer(baseUrl: string): Promise<boolean> {
  if (!(await isHealthy(baseUrl))) return false;
  return !(await compatibilityIssue(baseUrl));
}

async function canListenOnPort(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const server = createServer();
    server.unref();
    server.once("error", () => resolve(false));
    server.listen(port, "127.0.0.1", () => {
      server.close(() => resolve(true));
    });
  });
}

async function findAvailablePort(startPort: number, maxPort = startPort + 20): Promise<number> {
  for (let port = startPort; port <= maxPort; port += 1) {
    if (await canListenOnPort(port)) return port;
  }
  throw new Error(`Could not find a free local port between ${startPort} and ${maxPort}.`);
}

async function launchUrl(target: ServerTarget, existingServerWasHealthy: boolean): Promise<string> {
  const port = existingServerWasHealthy ? await findAvailablePort(target.preferredPort + 1) : target.preferredPort;
  const url = new URL(target.baseUrl);
  url.port = String(port);
  return url.toString().replace(/\/$/, "");
}

function launchLocalServer(baseUrl: string): LaunchedServer {
  const port = new URL(baseUrl).port;
  const serverDir = path.join(resolveRepoRoot(), "server");
  if (!existsSync(path.join(serverDir, "app", "main.py"))) {
    throw new Error(
      `Local server source was not found at ${serverDir}. Set SMITH_SERVER_URL to a running server or SMITH_REPO_ROOT to the Agent-Smith checkout.`,
    );
  }

  // A per-launch identity so the health probe can distinguish the server we
  // spawned from a foreign one that won the same port in a near-simultaneous
  // startup race (two shells, empty port, both spawn uvicorn — one loses the
  // bind and dies).  The server echoes SMITH_SERVER_NONCE from /api/health.
  const nonce = randomUUID();
  const child = spawn("uv", ["run", "uvicorn", "app.main:app", "--port", port], {
    cwd: serverDir,
    // Detached so `uv` leads its own process group: signalling the group (a
    // negative pid) also reaches the uvicorn grandchild that actually holds
    // the port.  Without this, killing the wrapper orphans uvicorn.
    detached: true,
    // stderr is piped, not ignored: first startup is where a port clash, a
    // broken venv or an import error shows up, and discarding uvicorn's own
    // message left the user with "exited before becoming healthy" and no way to
    // diagnose it. Bounded so a chatty server cannot grow this without limit.
    stdio: ["ignore", "ignore", "pipe"],
    env: { ...process.env, PYTHONUNBUFFERED: "1", SMITH_SERVER_NONCE: nonce },
  });
  let spawnError: Error | undefined;
  child.once("error", (error) => {
    spawnError = error;
  });
  const diagnostics: string[] = [];
  child.stderr?.setEncoding("utf8");
  child.stderr?.on("data", (chunk: string) => {
    diagnostics.push(chunk);
    if (diagnostics.length > STDERR_KEEP_CHUNKS) diagnostics.splice(0, diagnostics.length - STDERR_KEEP_CHUNKS);
  });

  ownedServer = child;
  registerCleanup();
  return { child, nonce, getSpawnError: () => spawnError, getStderrTail: () => stderrTail(diagnostics) };
}

async function healthNonce(baseUrl: string): Promise<string | null | undefined> {
  // undefined = probe failed/not-yet-up (retryable); null = healthy server with
  // no nonce (a foreign or manually started one); string = a server's identity.
  const timeout = createTimeoutSignal(SERVER_PROBE_TIMEOUT_MS);
  try {
    const response = await fetch(serverEndpoint(baseUrl, "/api/health"), { signal: timeout.signal });
    if (!response.ok) return undefined;
    const body = (await response.json()) as { nonce?: string | null };
    return body.nonce ?? null;
  } catch {
    return undefined;
  } finally {
    timeout.dispose();
  }
}

/** Append the server's own stderr, which is usually the whole diagnosis. */
function withServerDiagnostics(message: string, launch: LaunchedServer): string {
  const tail = launch.getStderrTail();
  return tail ? `${message}\n\nServer output:\n${tail}` : message;
}

async function waitForCompatibleServer(
  baseUrl: string,
  launch: LaunchedServer,
  priorServerWasHealthy: boolean,
): Promise<ServerConnection> {
  const startedAt = Date.now();
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (Date.now() - startedAt >= SERVER_STARTUP_TIMEOUT_MS) break;

    // Check OUR child's health before adopting whatever answers the port.  If
    // our spawn lost a bind race and died, a foreign but compatible server
    // replying here is not ours to keep — when it exits it takes our session
    // with it.  Fail loudly instead of silently riding someone else's server.
    const spawnError = launch.getSpawnError();
    if (spawnError) {
      throw new Error(`Could not launch the local Smith server: ${spawnError.message}`);
    }
    if (launch.child.exitCode !== null) {
      throw new Error(withServerDiagnostics("Local server exited before becoming healthy.", launch));
    }

    // Adopt the server only once it proves it is the one we spawned (our nonce)
    // AND is compatible.  A foreign server on the same port answers with a
    // different nonce (or none), so we keep waiting for our own child instead.
    const nonce = await healthNonce(baseUrl);
    if (nonce === launch.nonce && (await isCompatibleServer(baseUrl))) {
      return {
        baseUrl,
        started: true,
        note: priorServerWasHealthy
          ? `Found an older Smith server; started an isolated shell server on ${baseUrl}.`
          : undefined,
      };
    }
    await sleep(500);
  }

  cleanupOwnedServer();
  throw new Error(withServerDiagnostics("Timed out while starting the local Smith server.", launch));
}

export async function ensureLocalServer(): Promise<ServerConnection> {
  const target = serverTarget();
  assertSafeServerTarget(target);
  const existing = await inspectExistingServer(target);
  if (existing.connection) return existing.connection;
  if (target.envOverride) throw new Error(`Configured SMITH_SERVER_URL is unreachable: ${target.baseUrl}`);

  const baseUrl = await launchUrl(target, existing.healthy);
  const launch = launchLocalServer(baseUrl);
  const connection = await waitForCompatibleServer(baseUrl, launch, existing.healthy);
  if (!existing.issue) return connection;
  // Say why the server already on the port was not used, instead of silently
  // starting a second one.
  return {
    ...connection,
    note: connection.note
      ? `${connection.note} (${existing.issue})`
      : `Started an isolated shell server on ${baseUrl}; the server already running was incompatible: ${existing.issue}.`,
  };
}
