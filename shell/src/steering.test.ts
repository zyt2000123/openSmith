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

test("M7/M9: steerMessage locks channel across switch and no_active_run fallback does not block caller", async () => {
  const store = createAppStore();
  const mainSession = {
    id: "main-1",
    agent_id: "agent-1",
    title: "Main",
    created_at: "2026-11-25T00:00:00Z",
    message_count: 1,
    session_kind: "main" as const,
  };
  const sideSession = {
    id: "side-1",
    agent_id: "agent-1",
    title: "Side",
    created_at: "2026-11-25T00:00:01Z",
    message_count: 0,
    session_kind: "side" as const,
    parent_session_id: "main-1",
  };
  store.getState().set({
    baseUrl: "http://127.0.0.1:9999",
    agent: { id: "agent-1", name: "Smith", role: "personal-assistant", description: "" },
    currentSession: mainSession,
    recoverableRunId: "run-main",
    busy: true,
  });
  store.getState().pushTurn("main task");

  const bridge = new NodeBridge(store);
  const requests = (bridge as unknown as { activeRequests: Record<"main" | "side", AbortController | null> }).activeRequests;
  requests.main = new AbortController();

  let resolvePost!: (res: Response) => void;
  let postCount = 0;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => {
    postCount += 1;
    return new Promise<Response>((resolve) => {
      resolvePost = resolve;
    });
  }) as typeof fetch;

  try {
    // Initiate steer on main channel
    const steeringPromise = bridge.steerMessage("steer main while switching");
    // A second steer while POST is in-flight must queue rather than double-POST (M7)
    const secondResult = await bridge.steerMessage("second while in flight");
    assert.equal(secondResult, true);

    // Wait for localAuthHeaders() to settle and fetch() to be invoked once
    while (postCount === 0) {
      await new Promise((r) => setTimeout(r, 1));
    }
    assert.equal(postCount, 1);

    // Switch activeChannel to side BEFORE the POST resolves (M9)
    store.getState().switchChannel("side", {
      ...store.getState().getChannelState("main")!,
      session: sideSession,
      transcript: [],
      turnCount: 0,
      busy: false,
      queuedMessages: [],
    });

    resolvePost(
      new Response(
        JSON.stringify({
          accepted: true,
          status: "injected",
          run_id: "run-main",
          steer_id: "st_main_only",
          pending_count: 1,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    await steeringPromise;

    // Side transcript MUST remain empty; main (backgroundChannel) got the steer block!
    assert.equal(store.getState().transcript.length, 0);
    const bgTurn = store.getState().backgroundChannel?.transcript[0];
    assert.ok(bgTurn?.kind === "turn");
    assert.equal(bgTurn.blocks.some((b) => b.type === "steering" && b.steerId === "st_main_only"), true);
  } finally {
    globalThis.fetch = originalFetch;
  }

  // M7: no_active_run fallback triggers sendMessage without awaiting its completion
  store.getState().switchChannel("main");
  requests.main = new AbortController();
  let sendMessageStarted = false;
  let releaseSend!: () => void;
  (bridge as unknown as { sendMessage: (...args: unknown[]) => Promise<boolean> }).sendMessage = async () => {
    sendMessageStarted = true;
    await new Promise<void>((r) => {
      releaseSend = r;
    });
    return true;
  };
  globalThis.fetch = (async () => {
    // Simulate run finishing right as POST returns no_active_run
    requests.main = null;
    return new Response(
      JSON.stringify({ accepted: false, status: "no_active_run", run_id: null, steer_id: null, pending_count: 0 }),
      { status: 409, headers: { "Content-Type": "application/json" } },
    );
  }) as typeof fetch;

  try {
    const returned = await bridge.steerMessage("fallback after run end");
    assert.equal(returned, true);
    assert.equal(sendMessageStarted, true);
    releaseSend();
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("M8: reportRequestError with pendingApproval keeps tool interrupted as error instead of running", () => {
  const store = createAppStore();
  const bridge = new NodeBridge(store);
  store.getState().pushTurn("run shell");
  store.getState().applyEvent({ type: "tool_call", id: "t1", name: "shell", hint: "ls" });
  store.getState().applyEvent({
    type: "approval_required",
    runId: "run-1",
    approvalId: "appr-1",
    tool: "shell",
    level: "execute",
    reason: "needs approval",
    arguments: {},
  });

  const reportRequestError = (
    bridge as unknown as { reportRequestError: (error: unknown, channel?: "main" | "side") => void }
  ).reportRequestError.bind(bridge);
  reportRequestError(new Error("network drop"), "main");

  const state = store.getState();
  assert.equal(state.pendingApproval, null);
  const turn = state.transcript.find((e) => e.kind === "turn");
  assert.ok(turn?.kind === "turn");
  const toolBlock = turn.blocks.find((b) => b.type === "tool");
  assert.ok(toolBlock?.type === "tool");
  assert.equal(toolBlock.state, "error");
  // Approval system notice was also removed
  assert.equal(state.transcript.some((e) => e.kind === "system" && e.approvalId === "appr-1"), false);
});

test("M10: cancelRequest requeues unapplied steers and steering_deferred bypasses queue cap", async () => {
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
  store.getState().pushTurn("work");

  const bridge = new NodeBridge(store);
  const requests = (bridge as unknown as { activeRequests: Record<"main" | "side", AbortController | null> }).activeRequests;
  requests.main = new AbortController();

  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () =>
    new Response(
      JSON.stringify({ accepted: true, status: "injected", run_id: "run-1", steer_id: "st_cancel", pending_count: 1 }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    )) as typeof fetch;

  try {
    await bridge.steerMessage("keep me if cancelled");
    // Fill queue to MAX_QUEUED_MESSAGES first (3 items)
    bridge.enqueueMessage("q1");
    bridge.enqueueMessage("q2");
    bridge.enqueueMessage("q3");
    assert.equal(store.getState().queuedMessages.length, 3);

    // Cancel request -> unapplied steer MUST be moved into queuedMessages even though queue was full!
    assert.equal(bridge.cancelRequest(), true);
    assert.equal(store.getState().queuedMessages.length, 4);
    assert.equal(store.getState().queuedMessages[3]?.text, "keep me if cancelled");
  } finally {
    globalThis.fetch = originalFetch;
  }

  // Test steering_deferred when queue is already full (>= 3)
  const store2 = createAppStore();
  const bridge2 = new NodeBridge(store2);
  store2.getState().pushTurn("work2");
  bridge2.enqueueMessage("a");
  bridge2.enqueueMessage("b");
  bridge2.enqueueMessage("c");
  const consumeStream = (
    bridge2 as unknown as {
      consumeStream: (
        events: AsyncIterable<unknown>,
        signal: AbortSignal,
        channel?: "main" | "side",
      ) => Promise<unknown>;
    }
  ).consumeStream.bind(bridge2);

  async function* fakeDeferredStream() {
    yield {
      type: "steering_deferred",
      steerIds: ["st_late"],
      items: [{ steerId: "st_late", content: "late steer must not drop" }],
    };
    yield { type: "done", status: "completed", runId: "r2" };
  }

  await consumeStream(fakeDeferredStream(), new AbortController().signal, "main");
  assert.equal(store2.getState().queuedMessages.length, 4);
  assert.equal(store2.getState().queuedMessages[3]?.text, "late steer must not drop");
});
