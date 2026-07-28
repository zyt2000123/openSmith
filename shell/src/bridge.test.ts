import assert from "node:assert/strict";
import test from "node:test";

import { errorMessage, NodeBridge, terminalStatusMessage } from "./bridge.js";
import { MAX_QUEUED_MESSAGES } from "./queue.js";
import { createAppStore } from "./store.js";
import { createTurnEntry } from "./transcript-state.js";

test("error messages strip terminal control sequences before UI presentation", () => {
  const attack = `${String.fromCharCode(27)}]52;c;eA==${String.fromCharCode(7)}server failed`;

  assert.equal(errorMessage(new Error(attack)), "server failed");
});

test("request errors keep details in the transcript without duplicating them in the status line", () => {
  const store = createAppStore();
  const bridge = new NodeBridge(store);
  store.getState().pushTurn("run it");
  store.getState().applyEvent({ type: "tool_call", id: "tool-1", name: "shell", hint: "npm test" });
  const reportRequestError = (
    bridge as unknown as {
      reportRequestError: (error: unknown) => void;
    }
  ).reportRequestError.bind(bridge);

  reportRequestError(new Error("Request timed out after 120000ms."));

  const state = store.getState();
  const lastEntry = state.transcript.at(-1);
  const turn = state.transcript.at(-2);
  assert.ok(turn?.kind === "turn");
  const block = turn.blocks.find((candidate) => candidate.type === "tool");
  assert.ok(block?.type === "tool");
  assert.equal(block.state, "error");
  assert.deepEqual(state.toolActivity.running, {});
  assert.equal(lastEntry?.kind, "system");
  assert.equal(lastEntry?.kind === "system" && lastEntry.text, "[error] Request timed out after 120000ms.");
  assert.equal(state.statusLine, "Request failed. See the transcript for details.");
});

test("blocked runs are not described as model output limits", () => {
  assert.equal(terminalStatusMessage("incomplete", "blocked"), "Agent was blocked; see the transcript for the reason.");
  assert.equal(
    terminalStatusMessage("incomplete", "model_output_limit"),
    "Model output limit reached; the answer may be incomplete.",
  );
});

test("frontend message queue is capped and supports removal and clearing", () => {
  const store = createAppStore();
  const bridge = new NodeBridge(store);

  assert.equal(bridge.enqueueMessage("first"), true);
  assert.equal(bridge.enqueueMessage("second"), true);
  assert.equal(bridge.enqueueMessage("third"), true);
  assert.equal(bridge.enqueueMessage("fourth"), false);
  assert.equal(store.getState().queuedMessages.length, MAX_QUEUED_MESSAGES);

  const removed = bridge.removeQueuedMessage(1);
  assert.equal(removed?.text, "second");
  assert.deepEqual(
    store.getState().queuedMessages.map((item) => item.text),
    ["first", "third"],
  );

  assert.equal(bridge.removeLatestQueuedMessage()?.text, "third");
  assert.deepEqual(
    store.getState().queuedMessages.map((item) => item.text),
    ["first"],
  );
});

test("finishing a request starts the next queued message in FIFO order", async () => {
  const store = createAppStore();
  const bridge = new NodeBridge(store);
  bridge.enqueueMessage("second");
  bridge.enqueueMessage("third");

  const activeController = new AbortController();
  const started: string[] = [];
  const internal = bridge as unknown as {
    activeRequest: AbortController | null;
    finishRequest: (controller: AbortController) => void;
    sendMessage: (text: string, skillName?: string) => Promise<boolean>;
  };
  internal.activeRequest = activeController;
  internal.sendMessage = async (text) => {
    started.push(text);
    return true;
  };

  internal.finishRequest(activeController);
  await Promise.resolve();

  assert.deepEqual(started, ["second"]);
  assert.deepEqual(
    store.getState().queuedMessages.map((item) => item.text),
    ["third"],
  );
});

test("a rejected queue handoff releases the busy state instead of stalling the shell", async () => {
  const store = createAppStore();
  const bridge = new NodeBridge(store);
  bridge.enqueueMessage("second");
  store.getState().set({ busy: true, runStartedAt: 1, inputLocked: true });

  const activeController = new AbortController();
  const internal = bridge as unknown as {
    activeRequest: AbortController | null;
    finishRequest: (controller: AbortController) => void;
    sendMessage: (text: string, skillName?: string) => Promise<boolean>;
  };
  internal.activeRequest = activeController;
  // A shell operation (skill toggle, MCP refresh) locked the input while the run was ending.
  internal.sendMessage = async () => false;

  internal.finishRequest(activeController);
  await Promise.resolve();
  await Promise.resolve();

  const state = store.getState();
  assert.deepEqual(
    state.queuedMessages.map((item) => item.text),
    ["second"],
  );
  assert.equal(state.busy, false);
  assert.equal(state.runStartedAt, null);
});

test("a refused handoff returns the message to the head of the queue instead of dropping it", async () => {
  const store = createAppStore();
  const bridge = new NodeBridge(store);
  bridge.enqueueMessage("first");
  bridge.enqueueMessage("second");

  const activeController = new AbortController();
  const internal = bridge as unknown as {
    activeRequest: AbortController | null;
    finishRequest: (controller: AbortController) => void;
    sendMessage: (text: string, skillName?: string) => Promise<boolean>;
  };
  internal.activeRequest = activeController;
  // The user types while the handoff is still in flight, filling the queue to its cap.
  // Re-entering through enqueueMessage would be refused here, losing "first" outright.
  internal.sendMessage = async () => {
    bridge.enqueueMessage("typed-during-handoff");
    bridge.enqueueMessage("typed-after-that");
    assert.equal(store.getState().queuedMessages.length, MAX_QUEUED_MESSAGES);
    return false;
  };

  internal.finishRequest(activeController);
  await Promise.resolve();
  await Promise.resolve();

  assert.deepEqual(
    store.getState().queuedMessages.map((item) => item.text),
    ["first", "second", "typed-during-handoff", "typed-after-that"],
  );
});

test("starting a new session keeps the existing session in history", () => {
  const store = createAppStore();
  const session = {
    id: "session-1",
    agent_id: "agent-1",
    title: "old",
    created_at: "now",
    message_count: 1,
  };
  store.getState().set({ currentSession: session, sessions: [session], inputValue: "draft" });

  const bridge = new NodeBridge(store);

  assert.equal(bridge.startNewSession(), true);
  assert.equal(store.getState().currentSession, null);
  assert.equal(store.getState().sessions[0]?.id, "session-1");
});

test("cancelling a request allows a new session before the stream cleanup finishes", () => {
  const store = createAppStore();
  const bridge = new NodeBridge(store);
  const controller = new AbortController();
  const internal = bridge as unknown as { activeRequest: AbortController | null };
  internal.activeRequest = controller;
  store.getState().set({ busy: true, currentSession: null });

  assert.equal(bridge.cancelRequest(), true);
  assert.equal(internal.activeRequest, controller);
  assert.equal(bridge.startNewSession(), true);
});

test("cancelling a request settles every running tool in the transcript and HUD", () => {
  const store = createAppStore();
  const bridge = new NodeBridge(store);
  const controller = new AbortController();
  const internal = bridge as unknown as { activeRequest: AbortController | null };
  internal.activeRequest = controller;
  store.getState().pushTurn("run it");
  store.getState().applyEvent({ type: "tool_call", id: "tool-1", name: "shell", hint: "npm test" });
  store.getState().set({ busy: true });

  assert.equal(bridge.cancelRequest(), true);

  const state = store.getState();
  const turn = state.transcript.at(-1);
  assert.ok(turn?.kind === "turn");
  const block = turn.blocks.find((candidate) => candidate.type === "tool");
  assert.ok(block?.type === "tool");
  assert.equal(block.state, "cancelled");
  assert.equal(turn.streaming, false);
  assert.deepEqual(state.toolActivity.running, {});
});

test("leaving the token panel while it loads does not reopen it", async () => {
  const originalFetch = globalThis.fetch;
  let release!: (response: Response) => void;
  const response = new Promise<Response>((resolve) => {
    release = resolve;
  });
  globalThis.fetch = async () => response;

  const store = createAppStore();
  store.getState().set({ baseUrl: "http://127.0.0.1:8140" });
  const bridge = new NodeBridge(store);
  const loading = bridge.openTokenStats();
  assert.equal(store.getState().panel, "tokens");
  store.getState().set({ panel: "chat" });
  release(
    new Response(
      JSON.stringify({
        year: 2026,
        session_count: 0,
        active_days: 0,
        current_streak: 0,
        longest_streak: 0,
        favorite_model: null,
        peak_hour: null,
        input_tokens: 0,
        output_tokens: 0,
        total_tokens: 0,
        daily: [],
        models: [],
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ),
  );
  await loading;

  assert.equal(store.getState().panel, "chat");
  globalThis.fetch = originalFetch;
});

test("run explorer keeps recorded runs visible when optional health data is unavailable", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.includes("/observability/runs")) {
      return new Response(
        JSON.stringify([
          {
            run_id: "run-1",
            agent_id: "agent-1",
            created_at: "2026-01-01T00:00:00Z",
            finished_at: "2026-01-01T00:01:00Z",
            event_count: 3,
            tool_call_count: 1,
            backtrack_count: 0,
            approval_required_count: 0,
            total_tokens: 12,
          },
        ]),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (url.includes("/observability/health")) return new Response("unavailable", { status: 503 });
    if (url.includes("/observability/incidents")) {
      return new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } });
    }
    throw new Error(`Unexpected request: ${url}`);
  };

  try {
    const store = createAppStore();
    store.getState().set({ baseUrl: "http://127.0.0.1:8140" });
    await new NodeBridge(store).openRunExplorer();

    assert.equal(store.getState().panel, "runs");
    assert.equal(store.getState().observabilityRuns?.[0]?.run_id, "run-1");
    assert.equal(store.getState().observabilityHealth, null);
    assert.deepEqual(store.getState().observabilityIncidents, []);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("trace shows diagnosis when the optional improvement proposal is unavailable", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.endsWith("/diagnosis")) {
      return new Response(
        JSON.stringify({
          run_id: "run-1",
          agent_id: "agent-1",
          status: "needs_attention",
          summary: "A tool timed out.",
          evidence: ["tool=web_fetch"],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (url.endsWith("/improvement-proposal")) return new Response("unavailable", { status: 503 });
    throw new Error(`Unexpected request: ${url}`);
  };

  try {
    const store = createAppStore();
    store.getState().set({ baseUrl: "http://127.0.0.1:8140" });
    await new NodeBridge(store).showTrace("run-1");

    const lastEntry = store.getState().transcript.at(-1);
    assert.equal(lastEntry?.kind, "system");
    assert.match(lastEntry?.kind === "system" ? lastEntry.text : "", /A tool timed out/);
    assert.match(lastEntry?.kind === "system" ? lastEntry.text : "", /Proposal: unavailable/);
    assert.equal(store.getState().statusLine, "Trace for run-1.");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("clearing a session locks out new requests until deletion finishes", async () => {
  const originalFetch = globalThis.fetch;
  let release!: (response: Response) => void;
  const response = new Promise<Response>((resolve) => {
    release = resolve;
  });
  globalThis.fetch = async () => response;

  const store = createAppStore();
  const session = {
    id: "session-1",
    agent_id: "agent-1",
    title: "old",
    created_at: "now",
    message_count: 1,
  };
  store.getState().set({
    baseUrl: "http://127.0.0.1:8140",
    agent: { id: "agent-1", name: "Smith", role: "agent" },
    currentSession: session,
  });
  const bridge = new NodeBridge(store);
  const clearing = bridge.clearCurrentSession();

  assert.equal(store.getState().inputLocked, true);
  assert.equal(await bridge.sendMessage("must wait"), false);
  release(new Response(null, { status: 204 }));
  assert.equal(await clearing, true);
  assert.equal(store.getState().inputLocked, false);
  assert.equal(store.getState().currentSession, null);
  globalThis.fetch = originalFetch;
});

test("a failed resume keeps the current session and removes a stale 404 target", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response("gone", { status: 404, statusText: "Not Found" });

  const currentSession = {
    id: "current",
    agent_id: "agent-1",
    title: "current",
    created_at: "now",
    message_count: 1,
  };
  const staleSession = { ...currentSession, id: "stale", title: "stale" };
  const store = createAppStore();
  store.getState().set({
    baseUrl: "http://127.0.0.1:8140",
    currentSession,
    sessions: [currentSession, staleSession],
  });

  await new NodeBridge(store).resumeSession(staleSession);

  assert.equal(store.getState().currentSession?.id, "current");
  assert.deepEqual(
    store.getState().sessions.map((session) => session.id),
    ["current"],
  );
  assert.equal(store.getState().inputLocked, false);
  globalThis.fetch = originalFetch;
});

test("resuming a recoverable run replaces the partial reply instead of duplicating it", async () => {
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = async (input, init) => {
    const url = String(input);
    calls.push(`${init?.method ?? "GET"} ${url}`);
    if (url.endsWith("/api/agent/runs/run-1")) {
      return new Response(
        JSON.stringify({
          run_id: "run-1",
          agent_id: "agent-1",
          session_id: "session-1",
          status: "cancelled",
          created_at: "now",
          updated_at: "now",
          event_seq: 1,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (url.endsWith("/api/agent/sessions/session-1/messages")) {
      return new Response(
        JSON.stringify([
          { id: "user-1", session_id: "session-1", role: "user", content: "fix it", created_at: "now" },
          { id: "assistant-1", session_id: "session-1", role: "assistant", content: "partial", created_at: "now" },
        ]),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (url.endsWith("/api/agent/runs/run-1/resume")) {
      return new Response(
        'event: run_started\ndata: {"run_id":"run-1"}\n\nevent: message\ndata: {"text":"fresh"}\n\nevent: done\ndata: {"run_id":"run-1"}\n\n',
        {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        },
      );
    }
    if (url.endsWith("/api/agent/sessions")) {
      return new Response(
        JSON.stringify([{ id: "session-1", agent_id: "agent-1", title: "work", created_at: "now", message_count: 1 }]),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    throw new Error(`Unexpected request: ${url}`);
  };

  try {
    const session = { id: "session-1", agent_id: "agent-1", title: "work", created_at: "now", message_count: 1 };
    const store = createAppStore();
    store.getState().set({
      baseUrl: "http://127.0.0.1:8140",
      agent: { id: "agent-1", name: "Smith", role: "agent" },
      sessions: [session],
      recoverableRunId: "run-1",
    });

    await new NodeBridge(store).resumeRun();

    const turn = store.getState().transcript.find((entry) => entry.kind === "turn");
    assert.equal(turn?.kind === "turn" ? turn.assistantText : "", "fresh");
    assert.equal(store.getState().recoverableRunId, null);
    assert.equal(calls.includes("POST http://127.0.0.1:8140/api/agent/runs/run-1/resume"), true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("resuming a run in the current session restarts its counters instead of doubling them", async () => {
  const session = { id: "session-1", agent_id: "agent-1", title: "work", created_at: "now", message_count: 1 };
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.endsWith("/api/agent/runs/run-1")) {
      return new Response(
        JSON.stringify({
          run_id: "run-1",
          agent_id: "agent-1",
          session_id: "session-1",
          status: "cancelled",
          created_at: "now",
          updated_at: "now",
          event_seq: 1,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (url.endsWith("/api/agent/runs/run-1/resume")) {
      return new Response(
        'event: run_started\ndata: {"run_id":"run-1"}\n\nevent: token_usage\ndata: {"input_tokens":10,"output_tokens":5,"total_tokens":15}\n\nevent: done\ndata: {"run_id":"run-1","status":"completed"}\n\n',
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      );
    }
    if (url.endsWith("/api/agent/sessions")) {
      return new Response(JSON.stringify([session]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    throw new Error(`Unexpected request: ${url}`);
  };

  try {
    const store = createAppStore();
    store.getState().set({
      baseUrl: "http://127.0.0.1:8140",
      agent: { id: "agent-1", name: "Smith", role: "agent" },
      sessions: [session],
      // Same session as the interrupted run, so the resume path skips loadSession —
      // nothing else would clear the counters the replayed events add to.
      currentSession: session,
      recoverableRunId: "run-1",
      turnTokenUsage: { input_tokens: 100, output_tokens: 100, total_tokens: 200 },
    });
    store.getState().pushTurn("fix it");
    store.getState().applyEvent({ type: "tool_call", id: "t1", name: "shell", hint: "" });
    store
      .getState()
      .applyEvent({ type: "tool_result", id: "t1", error: false, blocked: false, preflight: false, summary: "ok" });

    await new NodeBridge(store).resumeRun();

    const state = store.getState();
    assert.equal(state.turnTokenUsage.total_tokens, 15);
    assert.equal(state.toolActivity.successes.shell, undefined);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("a rejected run resume preserves the existing partial reply", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.endsWith("/api/agent/runs/run-1")) {
      return new Response(
        JSON.stringify({
          run_id: "run-1",
          agent_id: "agent-1",
          session_id: "session-1",
          status: "incomplete",
          created_at: "now",
          updated_at: "now",
          event_seq: 1,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (url.endsWith("/api/agent/runs/run-1/resume")) {
      return new Response(JSON.stringify({ detail: "identity is no longer available" }), {
        status: 422,
        headers: { "Content-Type": "application/json" },
      });
    }
    throw new Error(`Unexpected request: ${url}`);
  };

  try {
    const session = { id: "session-1", agent_id: "agent-1", title: "work", created_at: "now", message_count: 1 };
    const store = createAppStore();
    store.getState().set({
      baseUrl: "http://127.0.0.1:8140",
      agent: { id: "agent-1", name: "Smith", role: "agent" },
      currentSession: session,
      sessions: [session],
      recoverableRunId: "run-1",
      transcript: [
        {
          ...createTurnEntry("fix it"),
          assistantText: "partial",
          streaming: false,
        },
      ],
    });

    await new NodeBridge(store).resumeRun();

    const turn = store.getState().transcript.find((entry) => entry.kind === "turn");
    assert.equal(turn?.kind === "turn" ? turn.assistantText : "", "partial");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("run recovery points users to the consolidated resume command", async () => {
  const store = createAppStore();

  await new NodeBridge(store).resumeRun();

  assert.equal(store.getState().statusLine, "No recoverable run is known. Use /resume run <run-id>.");
});

test("changing a session model locks out session switches until the patch finishes", async () => {
  const originalFetch = globalThis.fetch;
  let release!: (response: Response) => void;
  const response = new Promise<Response>((resolve) => {
    release = resolve;
  });
  globalThis.fetch = async () => response;

  const currentSession = {
    id: "current",
    agent_id: "agent-1",
    title: "current",
    created_at: "now",
    message_count: 1,
  };
  const store = createAppStore();
  store.getState().set({
    baseUrl: "http://127.0.0.1:8140",
    currentSession,
    config: {
      configured: true,
      has_api_key: true,
      provider: "openai",
      base_url: "https://relay.example/v1",
      model: "default-model",
      max_output_tokens: null,
      routes: {},
      timeout_profiles: {},
      models: { fast: { model: "fast-model", has_api_key: true } },
    },
  });
  const bridge = new NodeBridge(store);
  const selecting = bridge.selectModel("fast");

  assert.equal(store.getState().inputLocked, true);
  assert.equal(bridge.startNewSession(), false);
  release(
    new Response(JSON.stringify({ ...currentSession, model_profile: "fast" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
  await selecting;

  assert.equal(store.getState().inputLocked, false);
  assert.equal(store.getState().selectedModelProfile, "fast");
  globalThis.fetch = originalFetch;
});
