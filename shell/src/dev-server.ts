import { type ChildProcess, spawn } from "node:child_process";
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

type LaunchedServer = {
  child: ChildProcess;
  getSpawnError: () => Error | undefined;
};

let ownedServer: ChildProcess | null = null;
let cleanupRegistered = false;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function cleanupOwnedServer(): void {
  if (ownedServer?.exitCode === null) ownedServer.kill("SIGTERM");
  ownedServer = null;
}

function registerCleanup(): void {
  if (cleanupRegistered) return;

  cleanupRegistered = true;
  process.once("exit", cleanupOwnedServer);
  process.once("SIGINT", () => {
    cleanupOwnedServer();
    process.exit(130);
  });
  process.once("SIGTERM", () => {
    cleanupOwnedServer();
    process.exit(143);
  });
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
  const parsedUrl = new URL(baseUrl);
  const fallbackPort = parsedUrl.protocol === "https:" ? "443" : "80";
  return {
    baseUrl,
    envOverride: Boolean(process.env.SMITH_SERVER_URL),
    preferredPort: Number.parseInt(parsedUrl.port || fallbackPort, 10),
  };
}

async function isHealthy(baseUrl: string): Promise<boolean> {
  const timeout = createTimeoutSignal(SERVER_PROBE_TIMEOUT_MS);
  try {
    return (await fetch(`${baseUrl}/api/health`, { signal: timeout.signal })).ok;
  } catch {
    return false;
  } finally {
    timeout.dispose();
  }
}

async function compatibilityIssue(baseUrl: string): Promise<string | null> {
  const timeout = createTimeoutSignal(SERVER_PROBE_TIMEOUT_MS);
  try {
    const response = await fetch(`${baseUrl}/openapi.json`, { signal: timeout.signal });
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
): Promise<{ healthy: boolean; connection: ServerConnection | null }> {
  const healthy = await isHealthy(target.baseUrl);
  if (!healthy) return { healthy: false, connection: null };

  const issue = await compatibilityIssue(target.baseUrl);
  if (!issue) return { healthy: true, connection: { baseUrl: target.baseUrl, started: false } };
  if (target.envOverride) throw new Error(`Configured SMITH_SERVER_URL points to an incompatible server: ${issue}`);
  return { healthy: true, connection: null };
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

  const child = spawn("uv", ["run", "uvicorn", "app.main:app", "--port", port], {
    cwd: serverDir,
    stdio: "ignore",
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
  });
  let spawnError: Error | undefined;
  child.once("error", (error) => {
    spawnError = error;
  });

  ownedServer = child;
  registerCleanup();
  return { child, getSpawnError: () => spawnError };
}

async function waitForCompatibleServer(
  baseUrl: string,
  launch: LaunchedServer,
  priorServerWasHealthy: boolean,
): Promise<ServerConnection> {
  const startedAt = Date.now();
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (Date.now() - startedAt >= SERVER_STARTUP_TIMEOUT_MS) break;
    if (await isCompatibleServer(baseUrl)) {
      return {
        baseUrl,
        started: true,
        note: priorServerWasHealthy
          ? `Found an older Smith server; started an isolated shell server on ${baseUrl}.`
          : undefined,
      };
    }

    const spawnError = launch.getSpawnError();
    if (spawnError) {
      throw new Error(`Could not launch the local Smith server: ${spawnError.message}`);
    }
    if (launch.child.exitCode !== null) {
      throw new Error("Local server exited before becoming healthy.");
    }
    await sleep(500);
  }

  cleanupOwnedServer();
  throw new Error("Timed out while starting the local Smith server.");
}

export async function ensureLocalServer(): Promise<ServerConnection> {
  const target = serverTarget();
  const existing = await inspectExistingServer(target);
  if (existing.connection) return existing.connection;
  if (target.envOverride) throw new Error(`Configured SMITH_SERVER_URL is unreachable: ${target.baseUrl}`);

  const baseUrl = await launchUrl(target, existing.healthy);
  const launch = launchLocalServer(baseUrl);
  return waitForCompatibleServer(baseUrl, launch, existing.healthy);
}
