import assert from "node:assert/strict";
import test from "node:test";

import { createEmptyConversation } from "./conversation.js";

test("clearing a conversation removes the armed skill and all HUD state", () => {
  const conversation = createEmptyConversation("chat", "Conversation cleared. Next message starts a fresh session.");

  assert.equal(conversation.pendingSkill, null);
  assert.equal(conversation.currentSession, null);
  assert.equal(conversation.turnCount, 0);
  assert.deepEqual(conversation.contextUsage, {
    context_tokens: 0,
    context_window: 128_000,
    context_percent: 0,
    estimated: true,
  });
  assert.deepEqual(conversation.transcript, []);
  assert.equal(conversation.inputLocked, false);
  assert.equal(conversation.busy, false);
  assert.equal(conversation.compressing, false);
  assert.equal(conversation.runStartedAt, null);
  assert.equal(conversation.recoverableRunId, null);
  assert.equal(conversation.historyIndex, -1);
  assert.deepEqual(conversation.toolActivity, {
    calls: {},
    running: {},
    successes: {},
    errors: {},
    blocked: {},
    preflight: {},
  });
});
