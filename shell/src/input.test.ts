import assert from "node:assert/strict";
import test from "node:test";
import type { Key } from "ink";

import { NodeBridge } from "./bridge.js";
import {
  handleApprovalInput,
  handleCtrlC,
  handleEscape,
  handleHistoryNavigation,
  handleHooksNavigation,
  handleHooksSelection,
  handleModelPickerInput,
  handleQueuedEdit,
  handleSkillActionsSelection,
  handleSkillMentionNavigation,
  handleSkillMentionSelection,
  handleSkillsNavigation,
  handleSkillsSelection,
  handleSkillToggle,
  handleSlashNavigation,
  handleViewToggle,
  type ShellInputOptions,
} from "./input.js";
import { createModelPicker } from "./model-picker.js";
import { createAppStore } from "./store.js";

function escapeKey(): Key {
  return { escape: true } as Key;
}

function shiftLeftKey(): Key {
  return { leftArrow: true, shift: true } as Key;
}

function returnKey(): Key {
  return { return: true } as Key;
}

function inputOptions(bridge: NodeBridge, store: ReturnType<typeof createAppStore>): ShellInputOptions {
  return {
    mode: "chat",
    setupFlow: "initial",
    busy: true,
    compressing: false,
    inputLocked: false,
    viewMode: "compact",
    slashMenuOpen: false,
    slashItems: [],
    slashIndex: 0,
    skills: [],
    skillsIndex: 0,
    skillActionIndex: 0,
    hooksIndex: 0,
    skillMentionMenuOpen: false,
    skillMentions: [],
    skillMentionIndex: 0,
    panel: "chat",
    pendingSkill: null,
    configConfigured: true,
    exit: () => {},
    bridge,
    getState: store.getState,
  };
}

test("slash and skills navigation scrolls through the full lists", () => {
  const store = createAppStore();
  const options = inputOptions({} as NodeBridge, store);
  options.slashMenuOpen = true;
  options.slashItems = Array.from({ length: 10 }, (_, index) => ({
    id: `item-${index}`,
    title: `/item-${index}`,
    command: `/item-${index}`,
    description: "",
    category: "Commands",
  }));
  options.slashIndex = 8;

  assert.equal(handleSlashNavigation({ downArrow: true } as Key, options), true);
  assert.equal(store.getState().slashIndex, 9);

  options.slashMenuOpen = false;
  options.panel = "skills";
  options.skills = Array.from({ length: 6 }, (_, index) => ({
    name: `skill-${index}`,
    description: "",
    source: "builtin",
    version: "0.1.0",
    argument_hint: "",
  }));
  options.skillsIndex = 4;

  assert.equal(handleSkillsNavigation({ pageUp: true } as Key, options), true);
  assert.equal(store.getState().skillsIndex, 0);
});

test("Ctrl-O remounts Static so existing transcript entries use the selected view", () => {
  const store = createAppStore();
  const options = inputOptions(new NodeBridge(store), store);
  options.viewMode = "compact";

  assert.equal(handleViewToggle("o", { ctrl: true } as Key, options), true);
  assert.equal(store.getState().viewMode, "transcript");
  assert.equal(store.getState().transcriptEpoch, 1);
});

test("enter arms the selected skill, returns to chat, and inserts its @ mention", () => {
  const store = createAppStore();
  const options = inputOptions(new NodeBridge(store), store);
  options.busy = false;
  options.panel = "skills";
  options.skills = [
    {
      name: "research",
      description: "Research a topic.",
      source: "builtin",
      version: "0.1.0",
      argument_hint: "",
    },
  ];
  options.skillsIndex = 0;
  store.getState().set({ panel: "skills", inputValue: "" });

  assert.equal(handleSkillsSelection(returnKey(), options), true);
  assert.equal(store.getState().panel, "chat");
  assert.equal(store.getState().pendingSkill?.name, "research");
  assert.equal(store.getState().inputValue, "@research ");
  assert.equal(store.getState().statusLine, "");
});

test("skills menu opens the enablement list from its second action", () => {
  const store = createAppStore();
  const options = inputOptions(new NodeBridge(store), store);
  options.panel = "skill-actions";
  options.skillActionIndex = 1;
  store.getState().set({ panel: "skill-actions", skillActionIndex: 1 });

  assert.equal(handleSkillActionsSelection(returnKey(), options), true);
  assert.equal(store.getState().panel, "skill-toggle");
  assert.equal(store.getState().inputValue, "");
});

test("Enter toggles the selected skill with its next enabled state", () => {
  const store = createAppStore();
  const toggles: Array<{ name: string; enabled: boolean }> = [];
  const bridge = {
    setSkillEnabled: (name: string, enabled: boolean) => toggles.push({ name, enabled }),
  } as unknown as NodeBridge;
  const options = inputOptions(bridge, store);
  options.panel = "skill-toggle";
  options.skills = [
    {
      name: "research",
      description: "Research a topic.",
      source: "builtin",
      version: "0.1.0",
      argument_hint: "",
      enabled: true,
    },
  ];

  assert.equal(handleSkillToggle(returnKey(), options), true);
  assert.deepEqual(toggles, [{ name: "research", enabled: false }]);
});

test("hooks navigation opens the selected hook details", () => {
  const store = createAppStore();
  const options = inputOptions({} as NodeBridge, store);
  options.panel = "hooks";
  store.getState().set({ panel: "hooks", hooksIndex: 0 });

  assert.equal(handleHooksNavigation({ downArrow: true } as Key, options), true);
  assert.equal(store.getState().hooksIndex, 1);
  options.hooksIndex = 1;

  assert.equal(handleHooksSelection(returnKey(), options), true);
  assert.equal(store.getState().panel, "hook-details");
});

test("escape returns from hook details and then closes hooks", () => {
  const store = createAppStore();
  const options = inputOptions({} as NodeBridge, store);
  options.panel = "hook-details";
  store.getState().set({ panel: "hook-details" });

  assert.equal(handleEscape(escapeKey(), options), true);
  assert.equal(store.getState().panel, "hooks");

  options.panel = "hooks";
  assert.equal(handleEscape(escapeKey(), options), true);
  assert.equal(store.getState().panel, "chat");
});

test("@ skill picker navigates and inserts the selected skill into the input", () => {
  const store = createAppStore();
  const options = inputOptions(new NodeBridge(store), store);
  options.skillMentionMenuOpen = true;
  options.skillMentions = [
    {
      name: "research",
      description: "Research a topic.",
      source: "builtin",
      version: "0.1.0",
      argument_hint: "",
    },
    {
      name: "security-review",
      description: "Review a change.",
      source: "builtin",
      version: "0.1.0",
      argument_hint: "",
    },
  ];

  assert.equal(handleSkillMentionNavigation({ downArrow: true } as Key, options), true);
  assert.equal(store.getState().skillMentionIndex, 1);
  options.skillMentionIndex = 1;

  assert.equal(handleSkillMentionSelection(returnKey(), options), true);
  assert.equal(store.getState().inputValue, "@security-review ");
  assert.equal(store.getState().pendingSkill?.name, "security-review");
});

test("approval options move with arrows and Enter resolves the selected option", async () => {
  const store = createAppStore();
  const decisions: boolean[] = [];
  const bridge = {
    resolveApproval: async (approved: boolean) => {
      decisions.push(approved);
    },
  } as unknown as NodeBridge;
  const options = inputOptions(bridge, store);
  store.getState().set({
    pendingApproval: {
      runId: "run-1",
      approvalId: "approval-1",
      tool: "shell",
      level: "execute",
      reason: "Approval required",
      arguments: { command: "npm test" },
    },
  });

  assert.equal(handleApprovalInput({ downArrow: true } as Key, options), true);
  assert.equal(store.getState().approvalIndex, 1);
  assert.equal(handleApprovalInput(returnKey(), options), true);
  await Promise.resolve();
  assert.deepEqual(decisions, [false]);

  assert.equal(handleApprovalInput({ upArrow: true } as Key, options), true);
  assert.equal(store.getState().approvalIndex, 0);
  assert.equal(handleApprovalInput(returnKey(), options), true);
  await Promise.resolve();
  assert.deepEqual(decisions, [false, true]);
});

test("model picker confirms the selected review model only after choosing its role", async () => {
  const store = createAppStore();
  const selected: Array<{ model: string; target: string }> = [];
  const bridge = {
    applyDiscoveredModel: async (model: string, target: string) => {
      selected.push({ model, target });
    },
  } as unknown as NodeBridge;
  const options = inputOptions(bridge, store);
  store.getState().set({ modelPicker: createModelPicker(["GLM-5.2"]) });

  assert.equal(handleModelPickerInput(returnKey(), options), true);
  assert.equal(store.getState().modelPicker?.step, "target");

  assert.equal(handleModelPickerInput({ downArrow: true } as Key, options), true);
  assert.equal(handleModelPickerInput(returnKey(), options), true);
  assert.equal(store.getState().modelPicker?.step, "confirm");
  assert.equal(store.getState().modelPicker?.target, "review");

  assert.equal(handleModelPickerInput(returnKey(), options), true);
  await Promise.resolve();
  assert.deepEqual(selected, [{ model: "GLM-5.2", target: "review" }]);
});

test("model picker cancellation leaves the current config untouched", () => {
  const store = createAppStore();
  const options = inputOptions({} as NodeBridge, store);
  store.getState().set({
    modelPicker: {
      step: "confirm",
      models: ["GLM-5.2"],
      selectedIndex: 1,
      model: "GLM-5.2",
      target: "primary",
    },
  });

  assert.equal(handleModelPickerInput(returnKey(), options), true);
  assert.equal(store.getState().modelPicker, null);
});

test("Escape cancels the pending approval instead of consuming queued input", () => {
  const store = createAppStore();
  let cancelled = false;
  const bridge = {
    cancelRequest: () => {
      cancelled = true;
      return true;
    },
  } as unknown as NodeBridge;
  const options = inputOptions(bridge, store);
  store.getState().set({
    pendingApproval: {
      runId: "run-1",
      approvalId: "approval-1",
      tool: "shell",
      level: "execute",
      reason: "Approval required",
      arguments: { command: "npm test" },
    },
  });

  assert.equal(handleApprovalInput(escapeKey(), options), true);
  assert.equal(cancelled, true);
});

test("Ctrl-C during compression does not exit the shell", () => {
  const store = createAppStore();
  let exited = false;
  const options = inputOptions(new NodeBridge(store), store);
  options.compressing = true;
  options.exit = () => {
    exited = true;
  };

  assert.equal(handleCtrlC("c", { ctrl: true } as Key, options), true);
  assert.equal(exited, false);
});

test("approval input consumes Tab instead of switching panels", () => {
  const store = createAppStore();
  const options = inputOptions(new NodeBridge(store), store);
  store.getState().set({
    pendingApproval: {
      runId: "run-1",
      approvalId: "approval-1",
      tool: "shell",
      level: "execute",
      reason: "Approval required",
      arguments: { command: "npm test" },
    },
  });

  assert.equal(handleApprovalInput({ tab: true } as Key, options), true);
  assert.equal(store.getState().panel, "welcome");
});

test("escape removes queued messages from newest to oldest before cancelling", () => {
  const store = createAppStore();
  const bridge = new NodeBridge(store);
  bridge.enqueueMessage("first");
  bridge.enqueueMessage("second");
  bridge.enqueueMessage("third");
  const options = inputOptions(bridge, store);

  assert.equal(handleEscape(escapeKey(), options), true);
  assert.equal(handleEscape(escapeKey(), options), true);
  assert.equal(handleEscape(escapeKey(), options), true);
  assert.deepEqual(store.getState().queuedMessages, []);
});

test("escape cancels the running request once the queue is empty", () => {
  const store = createAppStore();
  let cancelled = false;
  const bridge = {
    removeLatestQueuedMessage: () => null,
    cancelRequest: () => {
      cancelled = true;
      return true;
    },
  } as unknown as NodeBridge;
  const options = inputOptions(bridge, store);

  assert.equal(handleEscape(escapeKey(), options), true);
  assert.equal(cancelled, true);
});

test("escape closes the slash palette without cancelling the task", () => {
  const store = createAppStore();
  let cancelled = false;
  const bridge = {
    removeLatestQueuedMessage: () => null,
    cancelRequest: () => {
      cancelled = true;
      return true;
    },
  } as unknown as NodeBridge;
  const options = inputOptions(bridge, store);
  options.busy = true;
  options.slashMenuOpen = true;
  store.getState().set({ inputValue: "/sk", slashIndex: 3 });

  assert.equal(handleEscape(escapeKey(), options), true);
  assert.equal(store.getState().inputValue, "");
  assert.equal(store.getState().slashIndex, 0);
  assert.equal(cancelled, false);

  options.slashMenuOpen = false;
  assert.equal(handleEscape(escapeKey(), options), true);
  assert.equal(cancelled, true);
});

test("escape returns from a skills list to the skills action menu", () => {
  const store = createAppStore();
  const options = inputOptions(new NodeBridge(store), store);
  options.busy = false;
  options.panel = "skills";
  const pendingSkill = {
    name: "research",
    description: "Research a topic.",
    source: "builtin",
    version: "0.1.0",
    argument_hint: "",
  };
  options.pendingSkill = pendingSkill;
  store.getState().set({ panel: "skills", inputValue: "", pendingSkill });

  assert.equal(handleEscape(escapeKey(), options), true);
  assert.equal(store.getState().panel, "skill-actions");
  assert.equal(store.getState().statusLine, "Back.");
  assert.equal(store.getState().pendingSkill?.name, "research");
});

test("escape clears a chat draft and armed skill", () => {
  const store = createAppStore();
  const options = inputOptions(new NodeBridge(store), store);
  options.busy = false;
  const pendingSkill = {
    name: "research",
    description: "Research a topic.",
    source: "builtin",
    version: "0.1.0",
    argument_hint: "",
  };
  options.pendingSkill = pendingSkill;
  store.getState().set({ inputValue: "draft", pendingSkill });

  assert.equal(handleEscape(escapeKey(), options), true);
  assert.equal(store.getState().inputValue, "");
  assert.equal(store.getState().pendingSkill, null);
  assert.equal(store.getState().statusLine, "Cleared.");
});

test("escape stays inert on the base chat panel", () => {
  const store = createAppStore();
  const options = inputOptions(new NodeBridge(store), store);
  options.busy = false;
  store.getState().set({ panel: "chat" });

  assert.equal(handleEscape(escapeKey(), options), false);
  assert.equal(store.getState().panel, "chat");
});

test("shift-left edits the newest queued message back into the input", () => {
  const store = createAppStore();
  const bridge = new NodeBridge(store);
  bridge.enqueueMessage("first");
  bridge.enqueueMessage("last message");
  const options = inputOptions(bridge, store);

  assert.equal(handleQueuedEdit(shiftLeftKey(), options), true);
  assert.equal(store.getState().inputValue, "last message");
  assert.deepEqual(
    store.getState().queuedMessages.map((item) => item.text),
    ["first"],
  );
});

test("Down leaves the draft untouched when not browsing history", () => {
  const store = createAppStore();
  const bridge = new NodeBridge(store);
  store.getState().set({ inputHistory: ["earlier message"], historyIndex: -1, inputValue: "important draft" });
  const options = inputOptions(bridge, store);

  assert.equal(handleHistoryNavigation({ downArrow: true } as Key, options), false);
  assert.equal(store.getState().inputValue, "important draft");
});

test("history navigation restores the draft that existed before browsing", () => {
  const store = createAppStore();
  store.getState().set({
    inputHistory: ["first message", "second message"],
    historyIndex: -1,
    inputValue: "important draft",
  });
  const options = inputOptions(new NodeBridge(store), store);

  assert.equal(handleHistoryNavigation({ upArrow: true } as Key, options), true);
  assert.equal(store.getState().inputValue, "second message");
  assert.equal(handleHistoryNavigation({ downArrow: true } as Key, options), true);
  assert.equal(store.getState().inputValue, "important draft");
  assert.equal(store.getState().historyIndex, -1);
});
