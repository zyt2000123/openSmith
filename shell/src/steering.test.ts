import assert from "node:assert/strict";
import test from "node:test";

import { NodeBridge } from "./bridge.js";
import { createAppStore } from "./store.js";
import { appendQueuedSteering, applyStreamEvent, createTurnEntry, type TranscriptEntry } from "./transcript-state.js";

test("steering_applied transitions queued steering block to applied and steering_deferred clears it", () => {
  let entries: TranscriptEntry[] = [createTurnEntry("refactor parser")];
  entries = appendQueuedSteering(entries, "st_1", "keep existing public API");

  const queuedTurn = entries[0];
  assert.ok(queuedTurn?.kind === "turn");
  assert.equal(queuedTurn.blocks.length, 1);
  assert.deepEqual(
    queuedTurn.blocks[0]?.type === "steering"
      ? { steerId: queuedTurn.blocks[0].steerId, state: queuedTurn.blocks[0].state }
      : null,
    { steerId: "st_1", state: "queued" },
  );

  entries = applyStreamEvent(entries, {
    type: "steering_applied",
    steerIds: ["st_1"],
    count: 1,
    items: [{ steerId: "st_1", content: "keep existing public API" }],
    iteration: 2,
  });
  const appliedTurn = entries[0];
  assert.ok(appliedTurn?.kind === "turn");
  assert.equal(appliedTurn.blocks[0]?.type === "steering" && appliedTurn.blocks[0].state, "applied");

  entries = appendQueuedSteering(entries, "st_2", "late note");
  entries = applyStreamEvent(entries, {
    type: "steering_deferred",
    steerIds: ["st_2"],
    items: [{ steerId: "st_2", content: "late note" }],
  });
  const afterDefer = entries[0];
  assert.ok(afterDefer?.kind === "turn");
  assert.equal(afterDefer.blocks.length, 1);
});

test("out-of-order parallel tool_call and tool_result converge accurately by call_id", () => {
  const store = createAppStore();
  store.getState().pushTurn("read 3 files");
  store.getState().applyEvent({ type: "tool_call", id: "c1", name: "read_file", hint: "a.ts" });
  store.getState().applyEvent({ type: "tool_call", id: "c2", name: "grep", hint: "foo" });
  store.getState().applyEvent({ type: "tool_call", id: "c3", name: "glob_files", hint: "*.ts" });

  assert.deepEqual(store.getState().toolActivity.running, {
    c1: "read_file",
    c2: "grep",
    c3: "glob_files",
  });

  // Complete c3 first, then c1, then c2
  store.getState().applyEvent({ type: "tool_result", id: "c3", error: false, blocked: false, preflight: false, summary: "3 files" });
  assert.deepEqual(store.getState().toolActivity.running, { c1: "read_file", c2: "grep" });

  store.getState().applyEvent({ type: "tool_result", id: "c1", error: false, blocked: false, preflight: false, summary: "ok" });
  assert.deepEqual(store.getState().toolActivity.running, { c2: "grep" });

  store.getState().applyEvent({ type: "tool_result", id: "c2", error: true, blocked: false, preflight: false, summary: "err" });
  assert.deepEqual(store.getState().toolActivity.running, {});

  const turn = store.getState().transcript[0];
  assert.ok(turn?.kind === "turn");
  const states = turn.blocks
    .filter((b) => b.type === "tool")
    .map((b) => [b.toolCallId, b.state]);
  assert.deepEqual(states, [
    ["c1", "success"],
    ["c2", "error"],
    ["c3", "success"],
  ]);
});

test("bridge.steerMessage injects into active run and falls back to queue on inbox_full", async () => {
  const store = createAppStore();
  store.getState().set({
    baseUrl: "http://127.0.0.1:9999",
    agent: { id: "agent-1", name: "Smith", role: "personal-assistant", description: "" },
    currentSession: {
      id: "sess-1",
      agent_id: "agent-1",
      title: "test",
      created_at: "2026-11-25T00:00:00Z",
      message_count: 1,
    },
    recoverableRunId: "run-1",
    busy: true,
  });
  store.getState().pushTurn("original");

  const bridge = new NodeBridge(store);
  (bridge as unknown as { activeRequest: AbortController | null }).activeRequest = new AbortController();

  const originalFetch = globalThis.fetch;
  let callIndex = 0;
  globalThis.fetch = (async () => {
    callIndex += 1;
    if (callIndex === 1) {
      return new Response(
        JSON.stringify({
          accepted: true,
          status: "injected",
          run_id: "run-1",
          steer_id: "st_abc",
          pending_count: 1,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    return new Response(
      JSON.stringify({
        accepted: false,
        status: "inbox_full",
        run_id: "run-1",
        steer_id: null,
        pending_count: 8,
      }),
      { status: 409, headers: { "Content-Type": "application/json" } },
    );
  }) as typeof fetch;

  try {
    const first = await bridge.steerMessage("don't edit tests");
    assert.equal(first, true);
    const turn = store.getState().transcript[0];
    assert.ok(turn?.kind === "turn");
    assert.equal(turn.blocks.some((b) => b.type === "steering" && b.steerId === "st_abc"), true);

    const second = await bridge.steerMessage("overflow message");
    assert.equal(second, true);
    assert.equal(store.getState().queuedMessages.length, 1);
    assert.equal(store.getState().queuedMessages[0]?.text, "overflow message");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
