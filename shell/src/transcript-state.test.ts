import assert from "node:assert/strict";
import test from "node:test";

import type { Message } from "./api.js";
import {
  applyStreamEvent,
  createSystemEntry,
  createTurnEntry,
  interruptLatestTurn,
  removeApprovalNotice,
  restoreTranscript,
  splitTranscript,
  type TranscriptEntry,
  type TurnEntry,
} from "./transcript-state.js";

function lastTurn(entries: TranscriptEntry[]): TurnEntry {
  const entry = entries[entries.length - 1];
  assert.equal(entry?.kind, "turn");
  return entry as TurnEntry;
}

function freshTurn(): TranscriptEntry[] {
  return [createTurnEntry("hello")];
}

test("message text accumulates and finalizes an open thinking block", () => {
  let entries = freshTurn();
  entries = applyStreamEvent(entries, { type: "thinking", text: "pondering", done: false });
  entries = applyStreamEvent(entries, { type: "message", text: "first " });
  entries = applyStreamEvent(entries, { type: "message", text: "second" });

  const turn = lastTurn(entries);
  assert.equal(turn.assistantText, "first second");
  assert.deepEqual(
    turn.blocks.map((block) => block.type === "thinking" && block.done),
    [true],
  );
});

test("smith-ui adds a structured presentation block without converting it to assistant text", () => {
  const entries = applyStreamEvent(freshTurn(), {
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
  });

  const turn = lastTurn(entries);
  assert.equal(turn.assistantText, "");
  assert.equal(turn.blocks.length, 1);
  assert.equal(turn.blocks[0]?.type, "smith_ui");
});

test("smith-ui fallback adds a CodeBlock payload without converting it to assistant text", () => {
  const entries = applyStreamEvent(freshTurn(), {
    type: "smith_ui_fallback",
    reason: "Unsupported smith-ui payload",
    code: '{\n  "type": "TextInput"\n}',
  });

  const turn = lastTurn(entries);
  assert.equal(turn.assistantText, "");
  assert.deepEqual(turn.blocks[0]?.type, "smith_ui_fallback");
});

test("provisional text retracts or commits by provision id", () => {
  let entries = freshTurn();
  entries = applyStreamEvent(entries, { type: "provisional_text_delta", provisionId: "draft-1", text: "discard" });

  let turn = lastTurn(entries);
  assert.equal(turn.assistantText, "");
  assert.deepEqual(turn.provisional, [{ provisionId: "draft-1", text: "discard" }]);

  entries = applyStreamEvent(entries, { type: "provisional_retract", provisionId: "draft-1", reason: "retry" });
  turn = lastTurn(entries);
  assert.equal(turn.assistantText, "");
  assert.deepEqual(turn.provisional, []);

  entries = applyStreamEvent(entries, { type: "provisional_text_delta", provisionId: "draft-2", text: "accepted" });
  entries = applyStreamEvent(entries, { type: "provisional_commit", provisionId: "draft-2" });
  turn = lastTurn(entries);
  assert.equal(turn.assistantText, "accepted");
  assert.deepEqual(turn.provisional, []);
});

test("thinking deltas merge into the open block; empty finished thinking is dropped", () => {
  let entries = freshTurn();
  entries = applyStreamEvent(entries, { type: "thinking", text: "step one", done: false });
  entries = applyStreamEvent(entries, { type: "thinking", text: "step two", done: false });

  let turn = lastTurn(entries);
  assert.equal(turn.blocks.length, 1);
  assert.equal(turn.blocks[0]?.type === "thinking" && turn.blocks[0].text, "step two");

  entries = applyStreamEvent(freshTurn(), { type: "thinking", text: "  ", done: false });
  turn = lastTurn(entries);
  assert.equal(turn.blocks.length, 0);
});

test("tool_call opens a running block and a repeated id re-marks it running", () => {
  let entries = freshTurn();
  entries = applyStreamEvent(entries, { type: "tool_call", id: "t1", name: "read", hint: "a.ts" });
  entries = applyStreamEvent(entries, {
    type: "tool_result",
    id: "t1",
    error: false,
    blocked: false,
    preflight: false,
    summary: "ok",
  });
  entries = applyStreamEvent(entries, { type: "tool_call", id: "t1", name: "read", hint: "b.ts" });

  const turn = lastTurn(entries);
  assert.equal(turn.blocks.length, 1);
  const block = turn.blocks[0];
  assert.ok(block?.type === "tool");
  assert.equal(block.state, "running");
  assert.equal(block.hint, "b.ts");
});

test("tool_result pairs with the latest matching call and maps status flags", () => {
  let entries = freshTurn();
  entries = applyStreamEvent(entries, { type: "tool_call", id: "t1", name: "bash", hint: "" });
  entries = applyStreamEvent(entries, {
    type: "tool_result",
    id: "t1",
    error: true,
    blocked: false,
    preflight: false,
    summary: "boom",
  });

  const turn = lastTurn(entries);
  const block = turn.blocks[0];
  assert.ok(block?.type === "tool");
  assert.equal(block.state, "error");
  assert.equal(block.summary, "boom");
});

test("an unmatched tool_result creates a fallback block instead of being dropped", () => {
  const entries = applyStreamEvent(freshTurn(), {
    type: "tool_result",
    id: "ghost",
    error: false,
    blocked: true,
    preflight: false,
    summary: "denied",
  });

  const turn = lastTurn(entries);
  const block = turn.blocks[0];
  assert.ok(block?.type === "tool");
  assert.equal(block.name, "tool");
  assert.equal(block.state, "blocked");
});

test("skill start/end events pair by name", () => {
  let entries = freshTurn();
  entries = applyStreamEvent(entries, { type: "skill", name: "debug", status: "start" });
  entries = applyStreamEvent(entries, {
    type: "tool_call",
    id: "t1",
    name: "read_file",
    hint: "engine/safety/tool_guard.py",
  });
  entries = applyStreamEvent(entries, {
    type: "tool_result",
    id: "t1",
    error: false,
    blocked: false,
    preflight: false,
    summary: "Read 120 lines",
  });
  entries = applyStreamEvent(entries, { type: "skill", name: "debug", status: "end" });

  const turn = lastTurn(entries);
  assert.equal(turn.blocks.length, 1);
  const block = turn.blocks[0];
  assert.ok(block?.type === "skill");
  assert.equal(block.state, "done");
  assert.equal(block.activities.length, 1);
  assert.deepEqual(block.activities[0], {
    id: "t1",
    name: "read_file",
    hint: "engine/safety/tool_guard.py",
    state: "success",
    summary: "Read 120 lines",
  });
});

test("workflow retries and blocks settle the active card without claiming success", () => {
  let entries = freshTurn();
  entries = applyStreamEvent(entries, { type: "skill", name: "planning", status: "start" });
  entries = applyStreamEvent(entries, { type: "skill", name: "planning", status: "retry" });

  let turn = lastTurn(entries);
  assert.ok(turn.blocks[0]?.type === "skill");
  assert.equal(turn.blocks[0]?.state, "retry");

  entries = applyStreamEvent(entries, { type: "skill", name: "planning", status: "start" });
  entries = applyStreamEvent(entries, { type: "skill", name: "planning", status: "blocked" });
  entries = applyStreamEvent(entries, { type: "done", status: "incomplete" });

  turn = lastTurn(entries);
  assert.deepEqual(
    turn.blocks.filter((block) => block.type === "skill").map((block) => block.state),
    ["blocked"],
  );
  assert.equal(turn.streaming, false);
});

test("SkillChain lifecycle stays visible while awaiting the user's reply", () => {
  let entries = freshTurn();
  entries = applyStreamEvent(entries, { type: "skill", name: "grilling", status: "start" });
  entries = applyStreamEvent(entries, {
    type: "route_decided",
    identityId: "coding",
    identityName: "Coding",
    routeId: "requirements-research",
    pipelineId: "requirements-research",
  });
  entries = applyStreamEvent(entries, {
    type: "gate_result",
    skill: "grilling",
    verdict: "retry",
    reason: "Need a target user.",
  });
  entries = applyStreamEvent(entries, {
    type: "backtrack",
    from: "research",
    to: "grilling",
    reason: "Scope is ambiguous.",
  });
  entries = applyStreamEvent(entries, {
    type: "awaiting_input",
    skill: "grilling",
    reason: "awaiting_user_input",
  });
  entries = applyStreamEvent(entries, {
    type: "done",
    status: "incomplete",
    reason: "awaiting_user_input",
  });

  const turn = entries.find((entry) => entry.kind === "turn");
  assert.ok(turn?.kind === "turn");
  assert.equal(turn.streaming, false);
  assert.equal(turn.blocks[0]?.type === "skill" && turn.blocks[0].state, "waiting");

  // Route / gate / backtrack / awaiting-input telemetry updates the skill
  // block state but never renders a separate system notice.
  const notices = entries.filter((entry) => entry.kind === "system").map((entry) => entry.text);
  assert.deepEqual(notices, []);
});

test("a direct route produces no route notice", () => {
  const entries = applyStreamEvent(freshTurn(), {
    type: "route_decided",
    identityId: "smith",
    identityName: "Smith",
    routeId: "direct",
    pipelineId: "",
  });

  const notices = entries.filter((entry) => entry.kind === "system");
  assert.equal(notices.length, 0);
});

test("a routed chain also produces no route notice", () => {
  const entries = applyStreamEvent(freshTurn(), {
    type: "route_decided",
    identityId: "coding",
    identityName: "Coding",
    routeId: "requirements-research",
    pipelineId: "requirements-research",
  });

  const notices = entries.filter((entry) => entry.kind === "system");
  assert.equal(notices.length, 0);
});

test("tool calls remain standalone when no workflow skill is running", () => {
  let entries = freshTurn();
  entries = applyStreamEvent(entries, {
    type: "tool_call",
    id: "t1",
    name: "read_file",
    hint: "engine/safety/tool_guard.py",
  });

  const turn = lastTurn(entries);
  assert.equal(turn.blocks.length, 1);
  assert.ok(turn.blocks[0]?.type === "tool");
});

test("done closes the streaming turn", () => {
  const entries = applyStreamEvent(freshTurn(), { type: "done", status: "completed" });
  assert.equal(lastTurn(entries).streaming, false);
});

test("approval notice is removed after the decision while the turn remains", () => {
  let entries = applyStreamEvent(freshTurn(), {
    type: "approval_required",
    runId: "run-1",
    approvalId: "approval-1",
    tool: "write_file",
    level: "write",
    reason: "Approval required",
    arguments: { path: "notes.md" },
  });

  assert.equal(
    entries.some((entry) => entry.kind === "system"),
    true,
  );
  entries = removeApprovalNotice(entries, "approval-1");
  assert.equal(
    entries.some((entry) => entry.kind === "system"),
    false,
  );
  assert.equal(
    entries.some((entry) => entry.kind === "turn"),
    true,
  );
});

test("stream events without any turn are no-ops", () => {
  const entries: TranscriptEntry[] = [createSystemEntry("note")];
  const next = applyStreamEvent(entries, { type: "message", text: "lost" });
  assert.equal(next, entries);
});

test("restoreTranscript merges consecutive assistant messages into one turn", () => {
  const messages: Message[] = [
    { id: "1", session_id: "s", role: "user", content: "hi", created_at: "" },
    { id: "2", session_id: "s", role: "assistant", content: "partial", created_at: "" },
    { id: "3", session_id: "s", role: "assistant", content: "full answer", created_at: "" },
    { id: "4", session_id: "s", role: "user", content: "next", created_at: "" },
  ];

  const entries = restoreTranscript(messages);
  assert.equal(entries.length, 2);
  const first = entries[0];
  assert.ok(first?.kind === "turn");
  assert.equal(first.assistantText, "partial\n\nfull answer");
  assert.equal(first.streaming, false);
});

test("restoreTranscript keeps a leading assistant message as an orphan turn", () => {
  const messages: Message[] = [{ id: "1", session_id: "s", role: "assistant", content: "hello", created_at: "" }];

  const entries = restoreTranscript(messages);
  assert.equal(entries.length, 1);
  const turn = entries[0];
  assert.ok(turn?.kind === "turn");
  assert.equal(turn.userText, "");
  assert.equal(turn.assistantText, "hello");
});

test("splitTranscript keeps the streaming turn and everything after it dynamic", () => {
  const closed = createTurnEntry("a");
  closed.streaming = false;
  const open = createTurnEntry("b");
  const entries: TranscriptEntry[] = [createSystemEntry("sys"), closed, open];

  const { done, active } = splitTranscript(entries);
  assert.deepEqual(
    done.map((entry) => entry.id),
    [entries[0]?.id, closed.id],
  );
  assert.deepEqual(
    active.map((entry) => entry.id),
    [open.id],
  );
});

// ── Audit 2026-07-26 P1: `done` must converge running blocks ──────────────

function turnWithRunningBlocks(): TranscriptEntry[] {
  let entries = freshTurn();
  entries = applyStreamEvent(entries, { type: "skill", name: "architecture", status: "start" });
  entries = applyStreamEvent(entries, { type: "tool_call", id: "c1", name: "shell", hint: "" });
  return entries;
}

function statefulBlocks(turn: TurnEntry) {
  return turn.blocks.filter((block) => block.type === "skill" || block.type === "tool");
}

test("done converges skill and tool blocks left running", () => {
  const entries = applyStreamEvent(turnWithRunningBlocks(), {
    type: "done",
    status: "completed",
  });

  const turn = lastTurn(entries);
  assert.equal(turn.streaming, false);
  const blocks = statefulBlocks(turn);
  assert.ok(blocks.length >= 1);
  for (const block of blocks) {
    assert.notEqual((block as { state: string }).state, "running", `${block.type} block left running`);
    // A tool call inside an active skill becomes one of its activities.
    for (const activity of (block as { activities?: { state: string }[] }).activities ?? []) {
      assert.notEqual(activity.state, "running", "skill activity left running");
    }
  }
});

test("done maps a failed run onto error state, not cancelled", () => {
  const entries = applyStreamEvent(turnWithRunningBlocks(), {
    type: "done",
    status: "failed",
  });

  const turn = lastTurn(entries);
  const skill = statefulBlocks(turn).find((block) => block.type === "skill");
  assert.equal((skill as { state: string } | undefined)?.state, "error");
});

test("done leaves already settled blocks untouched", () => {
  let entries = freshTurn();
  entries = applyStreamEvent(entries, { type: "tool_call", id: "c1", name: "shell", hint: "" });
  entries = applyStreamEvent(entries, {
    type: "tool_result",
    id: "c1",
    error: false,
    blocked: false,
    preflight: false,
    summary: "ok",
  });
  entries = applyStreamEvent(entries, { type: "done", status: "completed" });

  const turn = lastTurn(entries);
  const tool = statefulBlocks(turn).find((block) => block.type === "tool");
  assert.equal((tool as { state: string } | undefined)?.state, "success");
});

// ── Audit 2026-07-26 P2: a cancelled turn must keep the text the server stores ──

test("interrupt freezes the visible draft as assistant text", () => {
  // The server persists visible_provisional_reply as the assistant message, so
  // erasing it here would leave the user with a blank turn while the stored
  // session, and the next request's history, held text they never read.
  let entries = freshTurn();
  entries = applyStreamEvent(entries, {
    type: "provisional_text_delta",
    provisionId: "p1",
    text: "partial answer",
  });

  const interrupted = interruptLatestTurn(entries, "cancelled");

  const turn = lastTurn(interrupted);
  assert.equal(turn.assistantText, "partial answer");
  assert.deepEqual(turn.provisional, []);
});

test("interrupt does not overwrite committed assistant text with a draft", () => {
  let entries = freshTurn();
  entries = applyStreamEvent(entries, { type: "message", text: "committed" });
  entries = applyStreamEvent(entries, {
    type: "provisional_text_delta",
    provisionId: "p1",
    text: "draft",
  });

  const turn = lastTurn(interruptLatestTurn(entries, "error"));

  assert.equal(turn.assistantText, "committed");
});
