import assert from "node:assert/strict";
import test from "node:test";

import { NodeBridge } from "./bridge.js";
import { runShellCommand } from "./commands.js";
import { handleChannelToggle } from "./input.js";
import { createAppStore } from "./store.js";

test("background main channel continues accumulating stream events and approval while /side is active", async () => {
  const store = createAppStore();
  store.getState().set({
    baseUrl: "http://127.0.0.1:9999",
    agent: { id: "agent-1", name: "Smith", role: "personal-assistant", description: "" },
    currentSession: {
      id: "main-1",
      agent_id: "agent-1",
      title: "Main Task",
      created_at: "2026-11-25T00:00:00Z",
      message_count: 1,
      session_kind: "main",
    },
    busy: true,
    recoverableRunId: "run-main-1",
  });
  store.getState().pushTurn("main question");

  const bridge = new NodeBridge(store);
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () =>
    new Response(
      JSON.stringify({
        id: "side-1",
        agent_id: "agent-1",
        title: "Side chat",
        created_at: "2026-11-25T00:00:01Z",
        message_count: 0,
        session_kind: "side",
        parent_session_id: "main-1",
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    )) as typeof fetch;

  try {
    const opened = await bridge.openSideChat();
    assert.equal(opened, true);
    assert.equal(store.getState().activeChannel, "side");
    assert.equal(store.getState().currentSession?.id, "side-1");
    assert.equal(store.getState().backgroundChannel?.session?.id, "main-1");
    assert.equal(store.getState().backgroundChannel?.busy, true);

    // Main channel keeps receiving events in the background while side is active
    store.getState().applyEventForChannel("main", { type: "message", text: "background main chunk" });
    store.getState().applyEventForChannel("main", {
      type: "approval_required",
      runId: "run-main-1",
      approvalId: "appr-bg-1",
      tool: "shell",
      level: "execute",
      reason: "Needs approval",
      arguments: { command: "rm -rf tmp" },
    });

    // Side channel's own transcript is untouched, while backgroundChannel exposes main's pendingApproval
    assert.equal(store.getState().transcript.length, 0);
    assert.equal(store.getState().backgroundChannel?.pendingApproval?.approvalId, "appr-bg-1");

    // Switch back via /back -> restores main transcript & pendingApproval immediately
    const switchedBack = bridge.switchBackToMain();
    assert.equal(switchedBack, true);
    assert.equal(store.getState().activeChannel, "main");
    assert.equal(store.getState().currentSession?.id, "main-1");
    assert.equal(store.getState().pendingApproval?.approvalId, "appr-bg-1");
    const mainTurn = store.getState().transcript[0];
    assert.ok(mainTurn?.kind === "turn");
    assert.equal(mainTurn.assistantText, "background main chunk");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("/promote elevates side chat to main channel and clears background side state", async () => {
  const store = createAppStore();
  store.getState().set({
    baseUrl: "http://127.0.0.1:9999",
    agent: { id: "agent-1", name: "Smith", role: "personal-assistant", description: "" },
    currentSession: {
      id: "main-1",
      agent_id: "agent-1",
      title: "Main",
      created_at: "2026-11-25T00:00:00Z",
      message_count: 1,
      session_kind: "main",
    },
  });
  const bridge = new NodeBridge(store);

  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/side")) {
      return new Response(
        JSON.stringify({
          id: "side-42",
          agent_id: "agent-1",
          title: "Side chat",
          created_at: "2026-11-25T00:00:01Z",
          message_count: 0,
          session_kind: "side",
          parent_session_id: "main-1",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (url.endsWith("/promote")) {
      return new Response(
        JSON.stringify({
          id: "side-42",
          agent_id: "agent-1",
          title: "Promoted Architecture",
          created_at: "2026-11-25T00:00:01Z",
          message_count: 1,
          session_kind: "main",
          parent_session_id: "main-1",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    return new Response(
      JSON.stringify([
        {
          id: "side-42",
          agent_id: "agent-1",
          title: "Promoted Architecture",
          created_at: "2026-11-25T00:00:01Z",
          message_count: 1,
          session_kind: "main",
        },
      ]),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }) as typeof fetch;

  try {
    await runShellCommand("/side", { bridge, exit: () => {}, getState: () => store.getState() });
    assert.equal(store.getState().activeChannel, "side");

    // Ctrl+\ toggles back to main and again to side
    const toggledToMain = handleChannelToggle("\\", { ctrl: true } as never, {
      bridge,
      getState: () => store.getState(),
    } as never);
    assert.equal(toggledToMain, true);
    assert.equal(store.getState().activeChannel, "main");

    await runShellCommand("/promote Promoted Architecture", {
      bridge,
      exit: () => {},
      getState: () => store.getState(),
    });
    assert.equal(store.getState().activeChannel, "main");
    assert.equal(store.getState().currentSession?.id, "side-42");
    assert.equal(store.getState().currentSession?.title, "Promoted Architecture");
    assert.equal(store.getState().backgroundChannel, null);
    assert.equal(store.getState().sessions.some((s) => s.id === "side-42"), true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("H1: side channel blocks /resume, /new, /clear, /sessions and closeSideChat verifies session_kind", async () => {
  const store = createAppStore();
  const mainSession = {
    id: "main-1",
    agent_id: "agent-1",
    title: "Main",
    created_at: "2026-11-25T00:00:00Z",
    message_count: 2,
    session_kind: "main" as const,
  };
  const sideSession = {
    id: "side-9",
    agent_id: "agent-1",
    title: "Side chat",
    created_at: "2026-11-25T00:00:01Z",
    message_count: 0,
    session_kind: "side" as const,
    parent_session_id: "main-1",
  };
  store.getState().set({
    baseUrl: "http://127.0.0.1:9999",
    agent: { id: "agent-1", name: "Smith", role: "personal-assistant", description: "" },
    currentSession: mainSession,
    sessions: [mainSession],
  });
  store.getState().switchChannel("side", {
    ...store.getState().getChannelState("main")!,
    session: sideSession,
    transcript: [],
    turnCount: 0,
  });

  const bridge = new NodeBridge(store);
  const deletedUrls: string[] = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "DELETE") deletedUrls.push(String(input));
    return new Response(null, { status: 204 });
  }) as typeof fetch;

  try {
    // /resume, /new, /clear, /sessions must all be blocked in side chat
    await bridge.resumeSession(mainSession);
    assert.equal(store.getState().currentSession?.id, "side-9");
    await bridge.resumeRun("run-1");
    assert.equal(store.getState().currentSession?.id, "side-9");
    assert.equal(bridge.startNewSession(), false);
    assert.equal(await bridge.clearCurrentSession(), false);
    await runShellCommand("/sessions", { bridge, exit: () => {}, getState: () => store.getState() });
    assert.notEqual(store.getState().panel, "sessions");
    assert.equal(deletedUrls.length, 0);

    // Even if side session somehow held a main session, closeSideChat MUST NOT delete it
    store.getState().updateChannel("side", { session: mainSession });
    await bridge.closeSideChat();
    assert.equal(deletedUrls.length, 0);

    // Deleting a real side session passes expected_kind=side
    store.getState().switchChannel("side", {
      ...store.getState().getChannelState("main")!,
      session: sideSession,
      transcript: [],
      turnCount: 0,
    });
    await bridge.closeSideChat();
    assert.equal(deletedUrls.length, 1);
    assert.match(deletedUrls[0] ?? "", /\/api\/agent\/sessions\/side-9\?expected_kind=side$/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("dual concurrent streams on main and side run independently without cross-wiring", async () => {
  const store = createAppStore();
  const mainSession = {
    id: "main-c",
    agent_id: "agent-1",
    title: "Main",
    created_at: "2026-11-25T00:00:00Z",
    message_count: 0,
    session_kind: "main" as const,
  };
  const sideSession = {
    id: "side-c",
    agent_id: "agent-1",
    title: "Side",
    created_at: "2026-11-25T00:00:01Z",
    message_count: 0,
    session_kind: "side" as const,
    parent_session_id: "main-c",
  };
  store.getState().set({
    baseUrl: "http://127.0.0.1:9999",
    agent: { id: "agent-1", name: "Smith", role: "personal-assistant", description: "" },
    currentSession: mainSession,
  });

  const bridge = new NodeBridge(store);
  let releaseMain!: () => void;
  let mainStreamStarted!: () => void;
  const mainStartedPromise = new Promise<void>((r) => {
    mainStreamStarted = r;
  });

  const encoder = new TextEncoder();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/sessions/main-c/messages/stream")) {
      const stream = new ReadableStream({
        async start(controller) {
          controller.enqueue(encoder.encode('event: run_started\ndata: {"run_id":"r-main"}\n\n'));
          controller.enqueue(encoder.encode('event: message\ndata: {"text":"MAIN_CHUNK_1 "}\n\n'));
          mainStreamStarted();
          await new Promise<void>((r) => {
            releaseMain = r;
          });
          controller.enqueue(encoder.encode('event: message\ndata: {"text":"MAIN_CHUNK_2"}\n\n'));
          controller.enqueue(encoder.encode('event: done\ndata: {"status":"completed","run_id":"r-main"}\n\n'));
          controller.close();
        },
      });
      return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
    }
    if (url.includes("/sessions/main-c/side")) {
      return new Response(JSON.stringify(sideSession), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url.includes("/sessions/side-c/messages/stream")) {
      const stream = new ReadableStream({
        start(controller) {
          controller.enqueue(encoder.encode('event: run_started\ndata: {"run_id":"r-side"}\n\n'));
          controller.enqueue(encoder.encode('event: message\ndata: {"text":"SIDE_ANSWER"}\n\n'));
          controller.enqueue(encoder.encode('event: done\ndata: {"status":"completed","run_id":"r-side"}\n\n'));
          controller.close();
        },
      });
      return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
    }
    return new Response(JSON.stringify([mainSession]), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;

  try {
    // 1. Start main stream
    const mainPromise = bridge.sendMessage("main long task");
    await mainStartedPromise;

    // 2. While main is streaming, open /side and complete a side question
    await bridge.openSideChat("quick side question");
    const sideTurn = store.getState().transcript[0];
    assert.ok(sideTurn?.kind === "turn");
    assert.equal(sideTurn.assistantText, "SIDE_ANSWER");

    // 3. Finish main stream while still viewing side, then switch /back
    releaseMain();
    await mainPromise;
    bridge.switchBackToMain();

    const mainTurn = store.getState().transcript[0];
    assert.ok(mainTurn?.kind === "turn");
    assert.equal(mainTurn.assistantText, "MAIN_CHUNK_1 MAIN_CHUNK_2");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
