import assert from "node:assert/strict";
import test from "node:test";

import { createTimeoutSignal, decodeSseEvent, setSkillEnabled, streamMessage, streamRunResume } from "./api.js";

test("SSE decoder accepts standard data fields without a trailing space", () => {
  const event = decodeSseEvent('event: done\ndata:{"id":"message-1"}');

  assert.deepEqual(event, { type: "done", id: "message-1", status: "completed" });
});

test("SSE decoder exposes the run id when execution starts", () => {
  assert.deepEqual(decodeSseEvent('event: run_started\ndata: {"run_id":"run-1"}'), {
    type: "run_started",
    runId: "run-1",
  });
});

test("SSE decoder exposes context usage and compression state", () => {
  assert.deepEqual(
    decodeSseEvent(
      'event: context_usage\ndata: {"context_tokens":64000,"context_window":128000,"context_percent":58,"estimated":false,"message_tokens":60000,"tool_schema_tokens":3500,"protocol_tokens":500,"effective_context_window":128000,"safe_input_budget":110000,"output_reserve":4096,"safety_margin":13904,"window_declared":true,"output_limit_declared":true,"fit_status":"fit"}',
    ),
    {
      type: "context_usage",
      context_tokens: 64000,
      context_window: 128000,
      context_percent: 58,
      estimated: false,
      message_tokens: 60000,
      tool_schema_tokens: 3500,
      protocol_tokens: 500,
      effective_context_window: 128000,
      safe_input_budget: 110000,
      output_reserve: 4096,
      safety_margin: 13904,
      window_declared: true,
      output_limit_declared: true,
      fit_status: "fit",
    },
  );
  assert.deepEqual(decodeSseEvent('event: compression\ndata: {"active":true}'), {
    type: "compression",
    active: true,
  });
});

test("SSE decoder falls back from non-finite context usage values", () => {
  const event = decodeSseEvent('event: context_usage\ndata: {"context_percent":"not-a-number"}');

  assert.equal(event?.type, "context_usage");
  assert.equal(event?.type === "context_usage" && event.context_percent, 0);
});

test("SSE decoder exposes the SkillChain lifecycle", () => {
  assert.deepEqual(
    decodeSseEvent(
      'event: route_decided\ndata: {"identity_id":"coding","identity_name":"Coding","route_id":"requirements-research","pipeline_id":"requirements-research"}',
    ),
    {
      type: "route_decided",
      identityId: "coding",
      identityName: "Coding",
      routeId: "requirements-research",
      pipelineId: "requirements-research",
    },
  );
  assert.deepEqual(
    decodeSseEvent('event: gate_result\ndata: {"skill":"grilling","verdict":"retry","reason":"Need a target user."}'),
    { type: "gate_result", skill: "grilling", verdict: "retry", reason: "Need a target user." },
  );
  assert.deepEqual(
    decodeSseEvent('event: backtrack\ndata: {"from":"research","to":"grilling","reason":"Scope is ambiguous."}'),
    { type: "backtrack", from: "research", to: "grilling", reason: "Scope is ambiguous." },
  );
  assert.deepEqual(decodeSseEvent('event: awaiting_input\ndata: {"skill":"grilling","reason":"awaiting_user_input"}'), {
    type: "awaiting_input",
    skill: "grilling",
    reason: "awaiting_user_input",
  });
});

test("SSE decoder accepts a validated smith-ui event", () => {
  assert.deepEqual(
    decodeSseEvent(
      'event: smith_ui\ndata: {"version":1,"spec":{"root":"summary","elements":{"summary":{"type":"Heading","props":{"text":"Deployment","level":"h1"},"children":[]}}},"images":[]}',
    ),
    {
      type: "smith_ui",
      payload: {
        version: 1,
        spec: {
          root: "summary",
          elements: {
            summary: { type: "Heading", props: { text: "Deployment", level: "h1" }, children: [] },
          },
        },
        images: [],
      },
    },
  );
});

test("SSE decoder sends an invalid smith-ui event to the CodeBlock fallback", () => {
  const event = decodeSseEvent(
    'event: smith_ui\ndata: {"version":1,"spec":{"root":"input","elements":{"input":{"type":"TextInput","props":{},"children":[]}}},"images":[]}',
  );

  assert.equal(event?.type, "smith_ui_fallback");
  assert.equal(event?.type === "smith_ui_fallback" && event.reason, "Unsupported smith-ui payload");
  assert.match(event?.type === "smith_ui_fallback" ? event.code : "", /TextInput/);
});

test("request timeout signals abort and identify timeout rather than user cancellation", async () => {
  const request = createTimeoutSignal(5);
  try {
    await new Promise<void>((resolve, reject) => {
      request.signal.addEventListener("abort", () => {
        try {
          assert.equal(request.didTimeout(), true);
          assert.equal(request.signal.reason?.name, "TimeoutError");
          resolve();
        } catch (error) {
          reject(error);
        }
      });
    });
  } finally {
    request.dispose();
  }
});

test("SSE decoder preserves an incomplete terminal status", () => {
  const event = decodeSseEvent(
    'event: done\ndata: {"id":"message-1","status":"incomplete","reason":"model_output_limit"}',
  );

  assert.deepEqual(event, {
    type: "done",
    id: "message-1",
    status: "incomplete",
    reason: "model_output_limit",
  });
});

test("SSE decoder retains the run id on a terminal event", () => {
  assert.deepEqual(decodeSseEvent('event: done\ndata: {"id":"message-1","run_id":"run-1","status":"failed"}'), {
    type: "done",
    id: "message-1",
    runId: "run-1",
    status: "failed",
  });
});

test("streamRunResume posts to the run resume endpoint", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Array<{ url: string; method: string }> = [];
  globalThis.fetch = async (input, init) => {
    requests.push({ url: String(input), method: init?.method ?? "GET" });
    return new Response('event: done\ndata: {"run_id":"run-1"}\n\n', {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
  };

  try {
    const events = [];
    for await (const event of streamRunResume("http://127.0.0.1:8140", "run-1", { timeoutMs: 1_000 })) {
      events.push(event);
    }
    assert.deepEqual(requests, [{ url: "http://127.0.0.1:8140/api/agent/runs/run-1/resume", method: "POST" }]);
    assert.deepEqual(events, [{ type: "done", id: undefined, runId: "run-1", status: "completed" }]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("setSkillEnabled persists a skill toggle through the agent API", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Array<{ url: string; method: string; body: string | undefined }> = [];
  globalThis.fetch = async (input, init) => {
    requests.push({ url: String(input), method: init?.method ?? "GET", body: init?.body?.toString() });
    return Response.json({
      name: "research",
      description: "Research a topic.",
      source: "builtin",
      version: "0.1.0",
      argument_hint: "",
      enabled: false,
    });
  };

  try {
    const skill = await setSkillEnabled("http://127.0.0.1:8140", "research", false);
    assert.equal(skill.enabled, false);
    assert.deepEqual(requests, [
      {
        url: "http://127.0.0.1:8140/api/agent/skills/research",
        method: "PUT",
        body: '{"enabled":false}',
      },
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("HTTP error text is safe to present in the terminal", async () => {
  const originalFetch = globalThis.fetch;
  const attack = `${String.fromCharCode(27)}]52;c;eA==${String.fromCharCode(7)}request failed`;
  globalThis.fetch = async () => new Response(attack, { status: 502, statusText: "Bad Gateway" });

  try {
    await assert.rejects(
      setSkillEnabled("http://127.0.0.1:8140", "research", false),
      (error: unknown) => error instanceof Error && error.message === "HTTP 502: request failed",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("SSE decoding accepts CR-only line endings", () => {
  assert.deepEqual(decodeSseEvent('event: done\rdata: {"id":"message-1"}'), {
    type: "done",
    id: "message-1",
    status: "completed",
  });
});

test("streamMessage ignores events after the first terminal event", async () => {
  const originalFetch = globalThis.fetch;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        new TextEncoder().encode('event: done\ndata: {"id":"message-1"}\n\nevent: message\ndata: {"text":"stale"}\n\n'),
      );
    },
  });

  globalThis.fetch = async () =>
    new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });

  try {
    const events = [];
    for await (const event of streamMessage("http://127.0.0.1:8140", "session-1", "hello", { timeoutMs: 1_000 })) {
      events.push(event);
    }
    assert.deepEqual(events, [{ type: "done", id: "message-1", status: "completed" }]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("trailing token_usage after done is not dropped", async () => {
  const originalFetch = globalThis.fetch;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        new TextEncoder().encode(
          'event: done\ndata: {"id":"message-1"}\n\nevent: token_usage\ndata: {"input_tokens":1,"output_tokens":2,"total_tokens":3}\n\n',
        ),
      );
    },
  });

  globalThis.fetch = async () =>
    new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });

  try {
    const events = [];
    for await (const event of streamMessage("http://127.0.0.1:8140", "session-1", "hello", { timeoutMs: 1_000 })) {
      events.push(event);
    }
    // The terminal event is still the stream terminator; usage counters framed
    // after it in the same buffer must still reach the store.
    assert.deepEqual(events, [
      { type: "done", id: "message-1", status: "completed" },
      { type: "token_usage", input_tokens: 1, output_tokens: 2, total_tokens: 3 },
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("a token_usage frame split into a later read is yielded exactly once", async () => {
  // The frame arrives in a SEPARATE read after done — the TCP split the drain
  // loop exists for.  Without advancing the drain buffer past consumed chunks,
  // the next drain read and the final flush re-yield it, and applyStreamState
  // *adds* usage deltas, inflating the trailing frame 2-3x.
  const originalFetch = globalThis.fetch;
  let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      streamController = controller;
      controller.enqueue(new TextEncoder().encode('event: done\ndata: {"id":"message-1"}\n\n'));
    },
  });

  globalThis.fetch = async () =>
    new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });

  try {
    const events: Array<Record<string, unknown>> = [];
    const consume = (async () => {
      for await (const event of streamMessage("http://127.0.0.1:8140", "session-1", "hello", { timeoutMs: 1_000 })) {
        events.push(event as Record<string, unknown>);
      }
    })();

    // Deliver the trailing usage frame in its own read, then close the stream.
    await new Promise((resolve) => setTimeout(resolve, 10));
    streamController?.enqueue(
      new TextEncoder().encode(
        'event: token_usage\ndata: {"input_tokens":1,"output_tokens":2,"total_tokens":3}\n\n',
      ),
    );
    await new Promise((resolve) => setTimeout(resolve, 10));
    streamController?.close();
    await consume;

    const usageFrames = events.filter((event) => event.type === "token_usage");
    assert.equal(usageFrames.length, 1, "the split-out token_usage frame must be yielded exactly once");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("SSE decoder retains a tool preflight result", () => {
  const event = decodeSseEvent('event: tool_result\ndata: {"id":"tool-1","preflight":true,"summary":"facts first"}');

  assert.deepEqual(event, {
    type: "tool_result",
    id: "tool-1",
    error: false,
    blocked: false,
    preflight: true,
    summary: "facts first",
  });
});

test("SSE decoder exposes a user approval request with redacted arguments", () => {
  const event = decodeSseEvent(
    'event: approval_required\ndata: {"run_id":"run-1","approval_id":"approval-1","tool":"shell","level":"execute","reason":"Approval required","arguments":{"command":"git status"}}',
  );

  assert.deepEqual(event, {
    type: "approval_required",
    runId: "run-1",
    approvalId: "approval-1",
    tool: "shell",
    level: "execute",
    reason: "Approval required",
    arguments: { command: "git status" },
  });
});

test("SSE decoder preserves an optional structured approval presentation", () => {
  const event = decodeSseEvent(
    'event: approval_required\ndata: {"run_id":"run-1","approval_id":"approval-1","tool":"git_ops","level":"write","reason":"Approval required for git_ops","arguments":{"action":"commit"},"presentation":{"title":"Commit Git changes","summary":"Create a Git commit","details":[{"label":"Action","value":"commit"}],"reason":"This changes repository history."}}',
  );

  assert.deepEqual(event, {
    type: "approval_required",
    runId: "run-1",
    approvalId: "approval-1",
    tool: "git_ops",
    level: "write",
    reason: "Approval required for git_ops",
    arguments: { action: "commit" },
    presentation: {
      title: "Commit Git changes",
      summary: "Create a Git commit",
      details: [{ label: "Action", value: "commit" }],
      reason: "This changes repository history.",
    },
  });
});

test("SSE decoder preserves provisional lifecycle events", () => {
  assert.deepEqual(decodeSseEvent('event: provisional_text_delta\ndata: {"provision_id":"draft-1","text":"draft"}'), {
    type: "provisional_text_delta",
    provisionId: "draft-1",
    text: "draft",
  });
  assert.deepEqual(decodeSseEvent('event: provisional_retract\ndata: {"provision_id":"draft-1","reason":"retry"}'), {
    type: "provisional_retract",
    provisionId: "draft-1",
    reason: "retry",
  });
  assert.deepEqual(decodeSseEvent('event: provisional_commit\ndata: {"provision_id":"draft-2"}'), {
    type: "provisional_commit",
    provisionId: "draft-2",
  });
});

test("streamMessage stops after done even when the SSE body stays open", async () => {
  const originalFetch = globalThis.fetch;
  let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      streamController = controller;
      controller.enqueue(new TextEncoder().encode('event: done\ndata: {"id":"message-1"}\n\n'));
    },
  });

  globalThis.fetch = async () =>
    new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });

  const consume = (async () => {
    const events = [];
    for await (const event of streamMessage("http://127.0.0.1:8140", "session-1", "hello", { timeoutMs: 1_000 })) {
      events.push(event);
    }
    return events;
  })();

  try {
    const result = await Promise.race([
      consume,
      new Promise<"timeout">((resolve) => setTimeout(() => resolve("timeout"), 100)),
    ]);

    assert.notEqual(result, "timeout", "done should end the stream without waiting for socket close");
    assert.deepEqual(result, [{ type: "done", id: "message-1", status: "completed" }]);
  } finally {
    try {
      streamController?.close();
    } catch {
      // The fixed reader cancels the stream before this cleanup runs.
    }
    await consume.catch(() => undefined);
    globalThis.fetch = originalFetch;
  }
});

test("streamMessage recognizes a CR-only frame before the socket closes", async () => {
  const originalFetch = globalThis.fetch;
  let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      streamController = controller;
      controller.enqueue(new TextEncoder().encode('event: done\rdata: {"id":"message-1"}\r\r'));
    },
  });

  globalThis.fetch = async () =>
    new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });

  const consume = (async () => {
    const events = [];
    for await (const event of streamMessage("http://127.0.0.1:8140", "session-1", "hello", { timeoutMs: 1_000 })) {
      events.push(event);
    }
    return events;
  })();

  try {
    const result = await Promise.race([
      consume,
      new Promise<"timeout">((resolve) => setTimeout(() => resolve("timeout"), 100)),
    ]);

    assert.notEqual(result, "timeout", "a CR-only blank line must terminate the SSE frame");
    assert.deepEqual(result, [{ type: "done", id: "message-1", status: "completed" }]);
  } finally {
    try {
      streamController?.close();
    } catch {
      // The fixed reader cancels the stream before this cleanup runs.
    }
    await consume.catch(() => undefined);
    globalThis.fetch = originalFetch;
  }
});

test("stream timeout resets on SSE activity instead of limiting total run time", async () => {
  const originalFetch = globalThis.fetch;
  const encoder = new TextEncoder();
  let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      streamController = controller;
      controller.enqueue(encoder.encode('event: run_started\ndata: {"run_id":"run-1"}\n\n'));
      setTimeout(() => {
        controller.enqueue(encoder.encode('event: thinking\ndata: {"text":"still working","done":false}\n\n'));
      }, 30);
      setTimeout(() => {
        controller.enqueue(encoder.encode('event: done\ndata: {"id":"message-1"}\n\n'));
      }, 70);
    },
  });

  globalThis.fetch = async (_input, init) => {
    init?.signal?.addEventListener(
      "abort",
      () => {
        try {
          streamController?.error(init.signal?.reason);
        } catch {
          // The stream may already be closed after the done event.
        }
      },
      { once: true },
    );
    return new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
  };

  try {
    const events = [];
    for await (const event of streamMessage("http://127.0.0.1:8140", "session-1", "hello", { timeoutMs: 50 })) {
      events.push(event);
    }

    assert.deepEqual(events.at(-1), { type: "done", id: "message-1", status: "completed" });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("stream timeout still fails after an idle gap", async () => {
  const originalFetch = globalThis.fetch;
  let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      streamController = controller;
      controller.enqueue(new TextEncoder().encode('event: run_started\ndata: {"run_id":"run-1"}\n\n'));
    },
  });

  globalThis.fetch = async (_input, init) => {
    init?.signal?.addEventListener(
      "abort",
      () => {
        try {
          streamController?.error(init.signal?.reason);
        } catch {
          // The stream may already be closed after the done event.
        }
      },
      { once: true },
    );
    return new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
  };

  try {
    await assert.rejects(
      (async () => {
        for await (const _event of streamMessage("http://127.0.0.1:8140", "session-1", "hello", { timeoutMs: 20 })) {
          // Keep consuming until the idle timeout aborts the stream.
        }
      })(),
      /Request timed out after 20ms\./,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("streamMessage reassembles a CRLF frame separator split across read chunks", async () => {
  const originalFetch = globalThis.fetch;
  const encoder = new TextEncoder();
  // sse-starlette separates frames with CRLF. Split the done frame's first
  // `\r\n` across two reads (chunk A ends with `\r`, chunk B starts with `\n`)
  // to reproduce the phantom-boundary bug from per-chunk newline normalization.
  const frame = 'event: done\r\ndata: {"id":"message-1"}\r\n\r\n';
  const splitAt = frame.indexOf("\r\n") + 1;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(frame.slice(0, splitAt)));
      controller.enqueue(encoder.encode(frame.slice(splitAt)));
      controller.close();
    },
  });

  globalThis.fetch = async () =>
    new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });

  try {
    const events = [];
    for await (const event of streamMessage("http://127.0.0.1:8140", "session-1", "hello", { timeoutMs: 1_000 })) {
      events.push(event);
    }
    assert.deepEqual(events, [{ type: "done", id: "message-1", status: "completed" }]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("streamMessage rejects an unfinished SSE frame larger than 256 KiB", async () => {
  const originalFetch = globalThis.fetch;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(`data: ${"x".repeat(256 * 1024 + 1)}`));
      controller.close();
    },
  });
  globalThis.fetch = async () =>
    new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });

  try {
    await assert.rejects(
      (async () => {
        for await (const _event of streamMessage("http://127.0.0.1:8140", "session-1", "hello", { timeoutMs: 1_000 })) {
          // The malformed frame never produces an event.
        }
      })(),
      /SSE frame exceeded the 256 KiB limit\./,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// ── Untrusted text is sanitised at the decode boundary (audit P2, security) ──

const ESC_BYTE = String.fromCharCode(27);
const BEL_BYTE = String.fromCharCode(7);

function sseFrame(event: string, payload: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(payload)}`;
}

test("SSE decoder strips an OSC 52 clipboard write from assistant text", () => {
  const attack = `${ESC_BYTE}]52;c;bWFsaWNpb3Vz${BEL_BYTE}here is your answer`;

  const event = decodeSseEvent(sseFrame("message", { text: attack }));

  assert.deepEqual(event, { type: "message", text: "here is your answer" });
});

test("SSE decoder strips escape sequences from tool output and hints", () => {
  const result = decodeSseEvent(
    sseFrame("tool_result", { id: "c1", summary: `${ESC_BYTE}]7;file://x${BEL_BYTE}done` }),
  );
  assert.equal((result as { summary: string }).summary, "done");

  const call = decodeSseEvent(sseFrame("tool_call", { id: "c1", name: "shell", hint: `${ESC_BYTE}[2Jls -la` }));
  assert.equal((call as { hint: string }).hint, "ls -la");
});

test("SSE decoder strips escape sequences from streamed drafts and thinking", () => {
  const draft = decodeSseEvent(
    sseFrame("provisional_text_delta", { provision_id: "p1", text: `${ESC_BYTE}]0;t${BEL_BYTE}draft` }),
  );
  assert.equal((draft as { text: string }).text, "draft");

  const thinking = decodeSseEvent(
    sseFrame("thinking", { text: `${ESC_BYTE}]52;c;eA==${BEL_BYTE}pondering`, done: true }),
  );
  assert.equal((thinking as { text: string }).text, "pondering");
});

test("SSE decoder keeps an approval reason readable while stripping escapes", () => {
  const event = decodeSseEvent(
    sseFrame("approval_required", {
      run_id: "r1",
      approval_id: "a1",
      tool: "shell",
      level: "execute",
      reason: `${ESC_BYTE}]52;c;eA==${BEL_BYTE}Approval required for shell`,
      arguments: {},
    }),
  );

  assert.equal((event as { reason: string }).reason, "Approval required for shell");
});

test("SSE decoder sanitizes every structured approval presentation field", () => {
  const attack = `${ESC_BYTE}]52;c;eA==${BEL_BYTE}`;
  const event = decodeSseEvent(
    sseFrame("approval_required", {
      run_id: "r1",
      approval_id: "a1",
      tool: "shell",
      level: "execute",
      reason: "Approval required",
      arguments: {},
      presentation: {
        title: `${attack}Run command`,
        summary: `${attack}Smith wants to run a command.`,
        details: [{ label: `${attack}Command`, value: `${attack}git status` }],
        reason: `${attack}Need approval.`,
      },
    }),
  );

  assert.equal(event?.type, "approval_required");
  assert.deepEqual(event?.type === "approval_required" ? event.presentation : undefined, {
    title: "Run command",
    summary: "Smith wants to run a command.",
    details: [{ label: "Command", value: "git status" }],
    reason: "Need approval.",
  });
});

test("SSE decoder sanitizes Smith-UI fallback and error event text", () => {
  const attack = `${ESC_BYTE}]52;c;eA==${BEL_BYTE}`;
  const fallback = decodeSseEvent(
    sseFrame("smith_ui_fallback", { reason: `${attack}unsupported`, code: `${attack}{"safe":true}` }),
  );

  assert.deepEqual(fallback, {
    type: "smith_ui_fallback",
    reason: "unsupported",
    code: '{"safe":true}',
  });
  assert.throws(
    () => decodeSseEvent(sseFrame("error", { message: `${attack}stream failed` })),
    (error: unknown) => error instanceof Error && error.message === "stream failed",
  );
});

test("SSE decoder sanitizes skill and SkillChain lifecycle metadata before transcript rendering", () => {
  const attack = `${ESC_BYTE}]52;c;eA==${BEL_BYTE}`;

  assert.deepEqual(decodeSseEvent(sseFrame("skill", { name: `${attack}research`, status: `${attack}start` })), {
    type: "skill",
    name: "research",
    status: "start",
  });
  assert.deepEqual(
    decodeSseEvent(
      sseFrame("gate_result", {
        skill: `${attack}grilling`,
        verdict: `${attack}retry`,
        reason: `${attack}Need a target user.`,
      }),
    ),
    {
      type: "gate_result",
      skill: "grilling",
      verdict: "retry",
      reason: "Need a target user.",
    },
  );
});
