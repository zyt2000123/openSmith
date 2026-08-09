import assert from "node:assert/strict";
import test from "node:test";

import { ensureLocalServer, findMissingApiOperations, REQUIRED_API_OPERATIONS } from "./dev-server.js";

function compatiblePaths(): Record<string, Record<string, object>> {
  const paths: Record<string, Record<string, object>> = {};
  for (const operation of REQUIRED_API_OPERATIONS) {
    paths[operation.path] ??= {};
    paths[operation.path][operation.method.toLowerCase()] = {};
  }
  return paths;
}

async function assertConfiguredServerIsRejected(paths: Record<string, Record<string, object>>): Promise<void> {
  const originalFetch = globalThis.fetch;
  const originalServerUrl = process.env.SMITH_SERVER_URL;
  process.env.SMITH_SERVER_URL = "http://127.0.0.1:8140";
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.endsWith("/api/health")) return new Response("ok");
    if (url.endsWith("/openapi.json")) return Response.json({ paths });
    throw new Error(`Unexpected request: ${url}`);
  };

  try {
    await assert.rejects(ensureLocalServer(), /Configured SMITH_SERVER_URL points to an incompatible server/);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalServerUrl === undefined) delete process.env.SMITH_SERVER_URL;
    else process.env.SMITH_SERVER_URL = originalServerUrl;
  }
}

test("compatibility requires every API operation used by the shell", () => {
  const paths = compatiblePaths();
  delete paths["/api/agent/mcp"];

  assert.deepEqual(findMissingApiOperations(paths), ["GET /api/agent/mcp"]);
});

test("compatibility requires the memory maintenance endpoint polled by the HUD", () => {
  const paths = compatiblePaths();
  delete paths["/api/agent/memory/status"];

  assert.deepEqual(findMissingApiOperations(paths), ["GET /api/agent/memory/status"]);
});

test("compatibility rejects an API path with the wrong HTTP method", () => {
  const paths = compatiblePaths();
  paths["/api/agent/sessions/{session_id}/messages/stream"] = { get: {} };

  assert.deepEqual(findMissingApiOperations(paths), ["POST /api/agent/sessions/{session_id}/messages/stream"]);
});

test("startup rejects an explicitly configured server missing a shell API operation", async () => {
  const paths = compatiblePaths();
  delete paths["/api/agent/mcp"];

  await assertConfiguredServerIsRejected(paths);
});

test("a server whose health omits the staleness flag is rejected", async () => {
  const originalFetch = globalThis.fetch;
  const originalServerUrl = process.env.SMITH_SERVER_URL;
  process.env.SMITH_SERVER_URL = "http://127.0.0.1:8140";
  // Every operation the shell needs is present — this is exactly the server the
  // shape probe used to wave through: old enough to predate the flag, which is
  // itself the evidence that it is running code from another era.
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.endsWith("/api/health")) return Response.json({ status: "ok", version: "0.2.0" });
    if (url.endsWith("/openapi.json")) return Response.json({ paths: compatiblePaths() });
    throw new Error(`Unexpected request: ${url}`);
  };

  try {
    await assert.rejects(ensureLocalServer(), /too old to report whether its code is current/);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalServerUrl === undefined) delete process.env.SMITH_SERVER_URL;
    else process.env.SMITH_SERVER_URL = originalServerUrl;
  }
});

test("configured server URLs with a trailing slash preserve a sub-path prefix", async () => {
  const originalFetch = globalThis.fetch;
  const originalServerUrl = process.env.SMITH_SERVER_URL;
  const requests: string[] = [];
  process.env.SMITH_SERVER_URL = "http://127.0.0.1:8140/smith/";
  globalThis.fetch = async (input) => {
    const url = String(input);
    requests.push(url);
    if (url === "http://127.0.0.1:8140/smith/api/health") return Response.json({ status: "ok", stale: false });
    if (url === "http://127.0.0.1:8140/smith/openapi.json") return Response.json({ paths: compatiblePaths() });
    throw new Error(`Unexpected request: ${url}`);
  };

  try {
    await ensureLocalServer();
    // The third request is the staleness probe, which re-reads /api/health after
    // the API shape checks out — it has to keep the prefix too.
    assert.deepEqual(requests, [
      "http://127.0.0.1:8140/smith/api/health",
      "http://127.0.0.1:8140/smith/openapi.json",
      "http://127.0.0.1:8140/smith/api/health",
    ]);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalServerUrl === undefined) delete process.env.SMITH_SERVER_URL;
    else process.env.SMITH_SERVER_URL = originalServerUrl;
  }
});
