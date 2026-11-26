import assert from "node:assert/strict";
import test from "node:test";

import {
  buildRunProgressParts,
  displayContextPercent,
  elisionActivityPart,
  formatElapsed,
  MEMORY_FAILURE_STREAK_THRESHOLD,
  memoryMaintenanceLabel,
} from "./hud.js";
import { ERROR, MUTED, WARNING } from "./theme.js";

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

// ── Single-turn TaskBudget & Context Elision HUD ──

test("buildRunProgressParts renders three-dimensional budget progress and warning/error colors", () => {
  const parts = buildRunProgressParts(
    1_000,
    { input_tokens: 0, output_tokens: 0, total_tokens: 48_200 },
    75_000,
    {
      tokens_used: 48_200,
      tokens_limit: 400_000,
      subagent_tokens: 12_400,
      estimated_tokens: 0,
      wall_seconds: 74,
      wall_limit: 600,
      cost_usd: 1.7,
      cost_limit: 2.0,
      cost_unpriced: false,
      exhausted_dimension: null,
    },
  );
  const rendered = parts.map((part) => part.text).join("");
  assert.equal(rendered, "  working (1m 14s/10m · ↓ 48.2k/400.0k tok (sub 12.4k) · $1.70/$2.00)");
  // Cost is 85% of $2.00 -> WARNING color.
  const costSeg = parts.find((p) => p.text.includes("$1.70/$2.00"));
  assert.equal(costSeg?.color, WARNING);

  // Exhausted dimension renders in ERROR color; unpriced cost shows $?/... unpriced.
  const unpricedParts = buildRunProgressParts(
    1_000,
    { input_tokens: 0, output_tokens: 0, total_tokens: 390_000 },
    11_000,
    {
      tokens_used: 390_000,
      tokens_limit: 400_000,
      subagent_tokens: 0,
      estimated_tokens: 0,
      wall_seconds: 10,
      wall_limit: null,
      cost_usd: null,
      cost_limit: 2.0,
      cost_unpriced: true,
      exhausted_dimension: "tokens",
    },
  );
  assert.equal(unpricedParts.find((p) => p.text.includes("390.0k/400.0k"))?.color, ERROR);
  assert.ok(unpricedParts.some((p) => p.text.includes("$?/$2.00 unpriced")));
});

test("elisionActivityPart renders compact summary and stays empty when absent", () => {
  assert.deepEqual(elisionActivityPart(null), []);
  const parts = elisionActivityPart({
    trigger: "message_cap",
    elided_messages: 12,
    recorded_actions: 4,
    total_ledger_actions: 7,
    evicted_actions: 0,
    highlights: ["edit_file: engine/context/fitting.py"],
  });
  assert.equal(parts.length, 1);
  assert.equal(parts[0]?.[0]?.text, "✂ elided 12 msgs · 7 actions kept");
});
