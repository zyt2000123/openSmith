import assert from "node:assert/strict";
import test from "node:test";

import { createAppStore, TRANSCRIPT_LIMIT, TRANSCRIPT_TRIM_TARGET } from "./store.js";
import { splitTranscript } from "./transcript-state.js";

/**
 * Ink's <Static> renders `items.slice(index)` and only advances `index` from a
 * layout effect keyed on `items.length`. Mirroring that here shows what the
 * terminal actually receives across a sequence of store updates.
 */
function createStaticSpy() {
  let index = 0;
  let lastLength = -1;
  const printed: string[] = [];
  return {
    printed,
    /** `<Static key={transcriptEpoch}>` — a new key throws away the print cursor. */
    remount(): void {
      index = 0;
      lastLength = -1;
    },
    render(items: string[]): void {
      printed.push(...items.slice(index));
      if (items.length !== lastLength) {
        lastLength = items.length;
        index = items.length;
      }
    },
  };
}

test("system lines sanitize terminal control sequences at the display boundary", () => {
  const store = createAppStore();
  const attack = `${String.fromCharCode(27)}]52;c;eA==${String.fromCharCode(7)}request failed`;

  store.getState().pushSystemLine(attack, "error");

  const entry = store.getState().transcript.at(-1);
  assert.equal(entry?.kind === "system" ? entry.text : "", "request failed");
});

test("system lines still reach the terminal after the transcript hits its limit", () => {
  const store = createAppStore();
  const staticSpy = createStaticSpy();
  let epoch = store.getState().transcriptEpoch;
  const renderStatic = () => {
    const state = store.getState();
    if (state.transcriptEpoch !== epoch) {
      epoch = state.transcriptEpoch;
      staticSpy.remount();
    }
    const { done } = splitTranscript(state.transcript);
    staticSpy.render(["hero", ...done.map((entry) => (entry.kind === "system" ? entry.text : entry.id))]);
  };

  for (let index = 0; index < TRANSCRIPT_LIMIT; index += 1) {
    store.getState().pushSystemLine(`line-${index}`);
    renderStatic();
  }
  store.getState().pushSystemLine("overflow-marker");
  renderStatic();

  assert.equal(staticSpy.printed.includes("overflow-marker"), true);
});

test("transcript history is bounded for long-running shell sessions", () => {
  const store = createAppStore();

  for (let index = 0; index < TRANSCRIPT_LIMIT + 20; index += 1) {
    store.getState().pushSystemLine(`line-${index}`);
  }

  const transcript = store.getState().transcript;
  assert.equal(transcript.length <= TRANSCRIPT_LIMIT, true);
  // A trim cuts back to TRANSCRIPT_TRIM_TARGET, so the survivors start that far
  // back from the append that overflowed the limit.
  assert.equal(transcript[0]?.kind, "system");
  assert.equal(
    transcript[0]?.kind === "system" ? transcript[0].text : "",
    `line-${TRANSCRIPT_LIMIT + 1 - TRANSCRIPT_TRIM_TARGET}`,
  );
  const last = transcript.at(-1);
  assert.equal(last?.kind === "system" ? last.text : "", `line-${TRANSCRIPT_LIMIT + 19}`);
});

test("token usage tracks the current turn separately from the session total", () => {
  const store = createAppStore();

  store.getState().pushTurn("first");
  store.getState().applyEvent({ type: "token_usage", input_tokens: 120, output_tokens: 30, total_tokens: 150 });
  store.getState().applyEvent({ type: "token_usage", input_tokens: 10, output_tokens: 40, total_tokens: 50 });
  assert.equal(store.getState().turnTokenUsage.total_tokens, 200);
  assert.equal(store.getState().tokenUsage.total_tokens, 200);

  store.getState().pushTurn("second");
  assert.equal(store.getState().turnTokenUsage.total_tokens, 0);
  assert.equal(store.getState().tokenUsage.total_tokens, 200);

  store.getState().applyEvent({ type: "token_usage", input_tokens: 60, output_tokens: 20, total_tokens: 80 });
  assert.equal(store.getState().turnTokenUsage.total_tokens, 80);
  assert.equal(store.getState().tokenUsage.total_tokens, 280);
});

test("context usage replaces the HUD value and compression toggles input state", () => {
  const store = createAppStore();

  store.getState().applyEvent({
    type: "context_usage",
    context_tokens: 64_000,
    context_window: 128_000,
    context_percent: 50,
    estimated: false,
  });
  assert.equal(store.getState().contextUsage.context_percent, 50);

  store.getState().applyEvent({ type: "compression", active: true });
  assert.equal(store.getState().compressing, true);
  store.getState().applyEvent({ type: "compression", active: false });
  assert.equal(store.getState().compressing, false);
});

test("terminal approval notices are removed when a run terminates", () => {
  const store = createAppStore();
  store.getState().pushTurn("run it");
  store.getState().applyEvent({
    type: "approval_required",
    runId: "run-1",
    approvalId: "approval-1",
    tool: "shell",
    level: "execute",
    reason: "Approval required",
    arguments: { command: "npm test" },
  });

  store.getState().applyEvent({ type: "done", status: "failed" });

  assert.equal(store.getState().pendingApproval, null);
  assert.equal(
    store.getState().transcript.some((entry) => entry.kind === "system"),
    false,
  );
});

test("failed runs retain their id for recovery while completed runs clear it", () => {
  const store = createAppStore();

  store.getState().applyEvent({ type: "run_started", runId: "run-1" });
  store.getState().applyEvent({ type: "done", runId: "run-1", status: "failed" });
  assert.equal(store.getState().recoverableRunId, "run-1");

  store.getState().applyEvent({ type: "run_started", runId: "run-2" });
  store.getState().applyEvent({ type: "done", runId: "run-2", status: "completed" });
  assert.equal(store.getState().recoverableRunId, null);
});

// ── Audit 2026-07-26 P1: approval must never arrive behind a hidden panel ──

const APPROVAL = {
  type: "approval_required" as const,
  runId: "r1",
  approvalId: "a1",
  tool: "shell",
  level: "execute",
  reason: "Approval required for shell",
  arguments: {},
};

test("approval_required returns from a full-screen panel to chat", () => {
  const store = createAppStore();
  store.getState().set({ panel: "tokens" });

  store.getState().applyEvent(APPROVAL);

  assert.equal(store.getState().panel, "chat");
  assert.equal(store.getState().pendingApproval?.approvalId, "a1");
});

test("approval_required leaves a non-full-screen panel alone", () => {
  const store = createAppStore();
  store.getState().set({ panel: "welcome" });

  store.getState().applyEvent(APPROVAL);

  assert.equal(store.getState().panel, "welcome");
});

test("done clears the running tool activity map", () => {
  const store = createAppStore();
  store.getState().applyEvent({ type: "tool_call", id: "c1", name: "shell", hint: "" });
  assert.equal(Object.keys(store.getState().toolActivity.running).length, 1);

  store.getState().applyEvent({ type: "done", status: "completed" });

  assert.deepEqual(store.getState().toolActivity.running, {});
});

// ── Audit 2026-07-26 P3: approval and per-turn activity bookkeeping ──

test("a tool result ends the approval phase", () => {
  // On denial or the server's 300s timeout the engine reports a tool_result and
  // nothing else, so without this the prompt stayed on screen as a zombie whose
  // Enter 404'd and whose Esc killed the whole run.
  const store = createAppStore();
  store.getState().applyEvent(APPROVAL);
  assert.ok(store.getState().pendingApproval);

  store.getState().applyEvent({
    type: "tool_result",
    id: "c1",
    error: false,
    blocked: true,
    preflight: false,
    summary: "Approval timed out",
  });

  assert.equal(store.getState().pendingApproval, null);
  assert.equal(store.getState().approvalResolving, false);
});

test("a new turn clears per-call activity but keeps cumulative counts", () => {
  // Gateways that number call ids sequentially reuse "call_0" every turn; a
  // settled entry from the previous turn made the new call return early, so the
  // HUD showed no spinner and counted no success.
  const store = createAppStore();
  store.getState().applyEvent({ type: "tool_call", id: "call_0", name: "shell", hint: "" });
  store.getState().applyEvent({
    type: "tool_result",
    id: "call_0",
    error: false,
    blocked: false,
    preflight: false,
    summary: "ok",
  });
  assert.equal(store.getState().toolActivity.successes.shell, 1);

  store.getState().pushTurn("next question");

  assert.deepEqual(store.getState().toolActivity.calls, {});
  assert.deepEqual(store.getState().toolActivity.running, {});
  assert.equal(store.getState().toolActivity.successes.shell, 1, "cumulative counts must survive");

  // The reused id is now tracked again instead of being ignored.
  store.getState().applyEvent({ type: "tool_call", id: "call_0", name: "shell", hint: "" });
  assert.equal(Object.keys(store.getState().toolActivity.running).length, 1);
});
