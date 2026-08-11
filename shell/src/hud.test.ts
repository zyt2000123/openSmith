import assert from "node:assert/strict";
import test from "node:test";

import {
  buildRunProgressParts,
  displayContextPercent,
  formatElapsed,
  MEMORY_FAILURE_STREAK_THRESHOLD,
  memoryMaintenanceLabel,
} from "./hud.js";
import { MUTED } from "./theme.js";

test("formats active run duration for the status HUD", () => {
  assert.equal(formatElapsed(1_000, 1_000), "0s");
  assert.equal(formatElapsed(1_000, 60_999), "59s");
  assert.equal(formatElapsed(1_000, 181_000), "3m 0s");
});

test("keeps the run progress content aligned while removing its leading dot", () => {
  const parts = buildRunProgressParts(1_000, { input_tokens: 0, output_tokens: 0, total_tokens: 7_500 }, 11_000);

  assert.deepEqual(parts, [
    { text: "  ", color: MUTED },
    { text: "working ", color: MUTED },
    { text: "(10s", color: MUTED },
    { text: " · ↓ ", color: MUTED },
    { text: "7.5k tokens", color: MUTED },
    { text: ")", color: MUTED },
  ]);
  assert.equal(parts.map((part) => part.text).join(""), "  working (10s · ↓ 7.5k tokens)");
});

test("keeps the existing compact progress line when no token usage is available", () => {
  const parts = buildRunProgressParts(null, { input_tokens: 0, output_tokens: 0, total_tokens: 0 }, 11_000);

  assert.deepEqual(parts, [
    { text: "  ", color: MUTED },
    { text: "working ", color: MUTED },
    { text: "(0s", color: MUTED },
    { text: ")", color: MUTED },
  ]);
});

test("context percentage falls back safely when a compatible server returns a non-finite value", () => {
  assert.equal(displayContextPercent(Number.NaN), 0);
  assert.equal(displayContextPercent(Number.POSITIVE_INFINITY), 0);
  assert.equal(displayContextPercent(120.4), 100);
});

// ── Ambient memory maintenance (dreaming indicator) ──

test("memory label names the running pass, dreaming then curation then compilation", () => {
  assert.equal(memoryMaintenanceLabel({ compile: "running", dream: "running" }), "dreaming");
  assert.equal(memoryMaintenanceLabel({ compile: "running", dream: "idle" }), "compiling memory");
});

test("memory label prefers running work over queued work", () => {
  assert.equal(memoryMaintenanceLabel({ compile: "running", dream: "pending" }), "compiling memory");
});

test("memory label reports queued work when nothing is running", () => {
  assert.equal(memoryMaintenanceLabel({ compile: "pending", dream: "pending" }), "dream queued");
  assert.equal(memoryMaintenanceLabel({ compile: "pending", dream: "idle" }), "memory queued");
});

test("memory label shows nothing when idle or unavailable", () => {
  assert.equal(memoryMaintenanceLabel({ compile: "idle", dream: "idle" }), null);
  assert.equal(memoryMaintenanceLabel(null), null);
});

test("a trailing failure streak outranks every other memory label", () => {
  // "memory queued" would be actively misleading while every attempt fails.
  assert.equal(
    memoryMaintenanceLabel({
      compile: "pending",
      dream: "running",
      consecutive_failures: MEMORY_FAILURE_STREAK_THRESHOLD,
      last_error: "LLMResponseError: HTTP 401",
    }),
    `memory stalled ×${MEMORY_FAILURE_STREAK_THRESHOLD}`,
  );
});

test("failures below the streak threshold stay invisible", () => {
  // One or two failures are routine transients; the next tick retries them.
  assert.equal(
    memoryMaintenanceLabel({
      compile: "idle",
      dream: "idle",
      consecutive_failures: MEMORY_FAILURE_STREAK_THRESHOLD - 1,
      last_error: "boom",
    }),
    null,
  );
});

test("servers without the failure fields keep their old labels", () => {
  assert.equal(memoryMaintenanceLabel({ compile: "pending", dream: "idle" }), "memory queued");
});
