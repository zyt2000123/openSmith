import { type Key, useInput } from "ink";

import type { SkillSummary } from "./api.js";
import type { NodeBridge } from "./bridge.js";
import type { SlashItem } from "./commands.js";
import { LIFECYCLE_HOOKS } from "./hooks.js";
import {
  type ListNavigation,
  moveListIndex,
  SKILLS_PANEL_VISIBLE_ITEMS,
  SLASH_MENU_VISIBLE_ITEMS,
} from "./list-navigation.js";
import { advanceModelPicker, moveModelPicker } from "./model-picker.js";
import { fieldValue, isEditableSetupField, nextSetupIndex, setSetupField, setupFieldAt } from "./setup.js";
import { isSkillEnabled, selectedSkillMentionState } from "./skill-mention.js";
import type { AppStore, Mode, Panel } from "./store.js";
import { clearTerminal } from "./term.js";
import { TOKEN_TABS } from "./token-stats.js";
import type { TranscriptViewMode } from "./transcript-state.js";

export type ShellInputOptions = {
  mode: Mode;
  setupFlow: AppStore["setupFlow"];
  busy: boolean;
  compressing: boolean;
  inputLocked: boolean;
  viewMode: TranscriptViewMode;
  slashMenuOpen: boolean;
  slashItems: SlashItem[];
  slashIndex: number;
  skills: SkillSummary[];
  skillsIndex: number;
  skillActionIndex: number;
  hooksIndex: number;
  skillMentionMenuOpen: boolean;
  skillMentions: SkillSummary[];
  skillMentionIndex: number;
  panel: Panel;
  pendingSkill: SkillSummary | null;
  configConfigured: boolean;
  exit: () => void;
  bridge: NodeBridge;
  getState: () => AppStore;
};

function moveSetupSelection(options: ShellInputOptions, direction: 1 | -1): void {
  const state = options.getState();
  const field = setupFieldAt(state.setupIndex, state.setupFlow);
  const draft = isEditableSetupField(field)
    ? setSetupField(state.setupDraft, field, state.inputValue)
    : state.setupDraft;
  const index = nextSetupIndex(state.setupIndex, direction, true, state.setupFlow);
  const nextField = setupFieldAt(index, state.setupFlow);
  state.set({
    setupDraft: draft,
    setupIndex: index,
    inputValue: nextField === "save" ? "" : fieldValue(draft, nextField),
  });
}

function handleSetupInput(key: Key, options: ShellInputOptions): void {
  if (key.tab || key.downArrow) {
    moveSetupSelection(options, 1);
    return;
  }
  if (key.upArrow) {
    moveSetupSelection(options, -1);
    return;
  }
  if (key.escape && options.configConfigured) {
    options.getState().set({ mode: "chat", panel: "welcome", inputValue: "", statusLine: "Back." });
  }
}

export function handleCtrlC(input: string, key: Key, options: ShellInputOptions): boolean {
  if (!key.ctrl || input !== "c") return false;

  if (options.compressing || options.inputLocked) return true;
  if (options.busy && options.bridge.cancelRequest()) return true;

  options.exit();
  return true;
}

export function handleViewToggle(input: string, key: Key, options: ShellInputOptions): boolean {
  if (!key.ctrl || input !== "o") return false;

  const viewMode = options.viewMode === "compact" ? "transcript" : "compact";
  clearTerminal();
  const state = options.getState();
  state.set({ viewMode, transcriptEpoch: state.transcriptEpoch + 1, statusLine: `${viewMode} view.` });
  return true;
}

function navigationFromKey(key: Key): ListNavigation | null {
  if (key.upArrow) return "up";
  if (key.downArrow) return "down";
  if (key.pageUp) return "pageUp";
  if (key.pageDown) return "pageDown";
  if (key.home) return "home";
  if (key.end) return "end";
  return null;
}

export function handleSlashNavigation(key: Key, options: ShellInputOptions): boolean {
  if (!options.slashMenuOpen || options.slashItems.length === 0) return false;

  const state = options.getState();
  if (key.tab) {
    const selected = options.slashItems[options.slashIndex];
    if (selected) state.set({ inputValue: selected.command });
    return true;
  }
  const navigation = navigationFromKey(key);
  if (navigation) {
    state.set({
      slashIndex: moveListIndex(options.slashIndex, options.slashItems.length, navigation, SLASH_MENU_VISIBLE_ITEMS),
    });
    return true;
  }
  return false;
}

export function handleSkillsNavigation(key: Key, options: ShellInputOptions): boolean {
  if (
    options.slashMenuOpen ||
    (options.panel !== "skills" && options.panel !== "skill-toggle") ||
    options.skills.length === 0
  ) {
    return false;
  }

  const navigation = navigationFromKey(key);
  if (!navigation) return false;

  options.getState().set({
    skillsIndex: moveListIndex(options.skillsIndex, options.skills.length, navigation, SKILLS_PANEL_VISIBLE_ITEMS),
  });
  return true;
}

export function handleSkillActionsNavigation(key: Key, options: ShellInputOptions): boolean {
  if (options.panel !== "skill-actions") return false;

  const navigation = navigationFromKey(key);
  if (!navigation) return false;
  options.getState().set({ skillActionIndex: moveListIndex(options.skillActionIndex, 2, navigation, 2) });
  return true;
}

export function handleSkillActionsSelection(key: Key, options: ShellInputOptions): boolean {
  if (!key.return || options.panel !== "skill-actions") return false;

  options.getState().set({
    panel: options.skillActionIndex === 0 ? "skills" : "skill-toggle",
    inputValue: "",
    skillsIndex: 0,
    statusLine: "",
  });
  return true;
}

export function handleHooksNavigation(key: Key, options: ShellInputOptions): boolean {
  if (options.panel !== "hooks") return false;

  const navigation = navigationFromKey(key);
  if (!navigation) return false;
  options.getState().set({
    hooksIndex: moveListIndex(options.hooksIndex, LIFECYCLE_HOOKS.length, navigation, SKILLS_PANEL_VISIBLE_ITEMS),
  });
  return true;
}

export function handleHooksSelection(key: Key, options: ShellInputOptions): boolean {
  if (!key.return || options.panel !== "hooks") return false;

  options.getState().set({ panel: "hook-details", inputValue: "", statusLine: "" });
  return true;
}

export function handleSkillMentionNavigation(key: Key, options: ShellInputOptions): boolean {
  if (!options.skillMentionMenuOpen) return false;
  if (key.tab) return true;
  if (options.skillMentions.length === 0) return false;

  const navigation = navigationFromKey(key);
  if (!navigation) return false;

  options.getState().set({
    skillMentionIndex: moveListIndex(
      options.skillMentionIndex,
      options.skillMentions.length,
      navigation,
      SKILLS_PANEL_VISIBLE_ITEMS,
    ),
  });
  return true;
}

export function handleSkillMentionSelection(key: Key, options: ShellInputOptions): boolean {
  if (!key.return || !options.skillMentionMenuOpen || options.skillMentions.length === 0) return false;

  const selected = options.skillMentions[options.skillMentionIndex];
  if (!selected) return false;

  options.getState().set(selectedSkillMentionState(selected));
  return true;
}

export function handleSkillsSelection(key: Key, options: ShellInputOptions): boolean {
  if (!key.return || options.slashMenuOpen || options.panel !== "skills" || options.skills.length === 0) return false;

  const state = options.getState();
  if (state.inputValue.trim()) return false;

  const selected = options.skills[options.skillsIndex];
  if (!selected) return false;

  state.set({
    panel: "chat",
    inputValue: `@${selected.name} `,
    pendingSkill: selected,
    statusLine: "",
  });
  return true;
}

export function handleSkillToggle(key: Key, options: ShellInputOptions): boolean {
  if (!key.return || options.panel !== "skill-toggle" || options.skills.length === 0) return false;

  const selected = options.skills[options.skillsIndex];
  if (!selected) return false;

  void options.bridge.setSkillEnabled(selected.name, !isSkillEnabled(selected));
  return true;
}

function handleTokenNavigation(key: Key, options: ShellInputOptions): boolean {
  if (options.panel !== "tokens" || options.slashMenuOpen) return false;
  if (!key.leftArrow && !key.rightArrow) return false;

  const state = options.getState();
  const current = TOKEN_TABS.indexOf(state.tokenTab);
  const direction = key.leftArrow ? -1 : 1;
  const next = (current + direction + TOKEN_TABS.length) % TOKEN_TABS.length;
  state.set({ tokenTab: TOKEN_TABS[next] });
  return true;
}

export function handleApprovalInput(key: Key, options: ShellInputOptions): boolean {
  const state = options.getState();
  if (!state.pendingApproval) return false;

  if (key.tab) return true;

  if (key.upArrow || key.leftArrow || key.downArrow || key.rightArrow) {
    if (state.approvalResolving) return true;
    const direction = key.upArrow || key.leftArrow ? -1 : 1;
    state.set({ approvalIndex: (state.approvalIndex + direction + 2) % 2 });
    return true;
  }

  if (key.escape) {
    if (!state.approvalResolving) options.bridge.cancelRequest();
    return true;
  }
  if (!key.return) return false;
  if (!state.approvalResolving) void options.bridge.resolveApproval(state.approvalIndex === 0);
  return true;
}

export function handleModelPickerInput(key: Key, options: ShellInputOptions): boolean {
  const state = options.getState();
  const picker = state.modelPicker;
  if (!picker) return false;

  if (key.tab) return true;
  if (key.escape) {
    state.set({ modelPicker: null, statusLine: "Model change cancelled." });
    return true;
  }

  const navigation = navigationFromKey(key);
  if (navigation) {
    state.set({ modelPicker: moveModelPicker(picker, navigation) });
    return true;
  }
  if (!key.return) return false;

  const next = advanceModelPicker(picker);
  if (next.selection) {
    void options.bridge.applyDiscoveredModel(next.selection.model, next.selection.target);
    return true;
  }
  state.set({
    modelPicker: next.picker,
    statusLine: next.picker ? "Choose where to configure this model." : "Model change cancelled.",
  });
  return true;
}

export function handleEscape(key: Key, options: ShellInputOptions): boolean {
  if (!key.escape) return false;

  const state = options.getState();
  if (options.skillMentionMenuOpen) {
    state.set({ inputValue: "", skillMentionIndex: 0 });
    return true;
  }
  if (options.slashMenuOpen) {
    state.set({ inputValue: "", slashIndex: 0 });
    return true;
  }
  if (options.panel === "hook-details") {
    state.set({ panel: "hooks", inputValue: "", statusLine: "Back." });
    return true;
  }
  if (options.panel === "skills" || options.panel === "skill-toggle") {
    state.set({ panel: "skill-actions", inputValue: "", skillsIndex: 0, statusLine: "Back." });
    return true;
  }
  if (options.panel === "skill-actions") {
    state.set({ panel: "chat", inputValue: "", statusLine: "Back." });
    return true;
  }
  if (options.panel !== "chat") {
    state.set({ panel: "chat", inputValue: "", statusLine: "Back." });
    return true;
  }
  if (options.pendingSkill || state.inputValue) {
    state.set({ pendingSkill: null, inputValue: "", statusLine: "Cleared." });
    return true;
  }
  if (options.bridge.removeLatestQueuedMessage()) return true;
  if (options.busy && options.bridge.cancelRequest()) return true;
  return false;
}

export function handleQueuedEdit(key: Key, options: ShellInputOptions): boolean {
  if (!key.shift || !key.leftArrow) return false;

  const queued = options.bridge.removeLatestQueuedMessage();
  if (!queued) return false;

  const state = options.getState();
  const skill = queued.skillName ? state.skills.find((item) => item.name === queued.skillName) : undefined;
  state.set({ inputValue: queued.text, ...(skill ? { pendingSkill: skill } : {}) });
  return true;
}

export function handleHistoryNavigation(key: Key, options: ShellInputOptions): boolean {
  if (
    options.panel !== "chat" ||
    options.slashMenuOpen ||
    options.skillMentionMenuOpen ||
    (!key.upArrow && !key.downArrow)
  ) {
    return false;
  }

  const state = options.getState();
  if (state.inputHistory.length === 0) return false;

  if (key.upArrow) {
    const index = state.historyIndex === -1 ? state.inputHistory.length - 1 : Math.max(0, state.historyIndex - 1);
    state.set({
      historyIndex: index,
      historyDraft: state.historyIndex === -1 ? state.inputValue : state.historyDraft,
      inputValue: state.inputHistory[index] || "",
    });
    return true;
  }

  if (state.historyIndex === -1) return false;
  const index = state.historyIndex + 1;
  if (index >= state.inputHistory.length) {
    state.set({ historyIndex: -1, historyDraft: "", inputValue: state.historyDraft });
  } else {
    state.set({ historyIndex: index, inputValue: state.inputHistory[index] || "" });
  }
  return true;
}

function cyclePanel(key: Key, options: ShellInputOptions): void {
  if (!key.tab || options.slashMenuOpen) return;

  const panels: Panel[] = ["welcome", "sessions", "skill-actions", "mcp", "hooks", "tokens", "runs", "chat"];
  const index = panels.indexOf(options.panel);
  const next = panels[(index + 1) % panels.length];

  // The data-backed panels load through the bridge; setting `panel` alone would
  // strand them on their loading placeholder with no request behind it.
  if (next === "tokens") {
    void options.bridge.openTokenStats();
    return;
  }
  if (next === "runs") {
    void options.bridge.openRunExplorer();
    return;
  }
  options.getState().set({ panel: next });
  if (next === "mcp") void options.bridge.refreshMcpServers();
}

function handlePickerInput(key: Key, options: ShellInputOptions): boolean {
  if (handleHooksSelection(key, options)) return true;
  if (handleHooksNavigation(key, options)) return true;
  if (handleSkillActionsSelection(key, options)) return true;
  if (handleSkillActionsNavigation(key, options)) return true;
  if (handleSkillToggle(key, options)) return true;
  if (handleSkillMentionSelection(key, options)) return true;
  if (handleSlashNavigation(key, options)) return true;
  if (handleSkillMentionNavigation(key, options)) return true;
  if (handleSkillsSelection(key, options)) return true;
  return handleSkillsNavigation(key, options);
}

function routeInput(input: string, key: Key, options: ShellInputOptions): void {
  if (handleCtrlC(input, key, options)) return;
  if (options.compressing || options.inputLocked) return;
  if (options.mode === "setup") {
    handleSetupInput(key, options);
    return;
  }
  if (handleModelPickerInput(key, options)) return;
  if (handleApprovalInput(key, options)) return;
  if (handleViewToggle(input, key, options)) return;
  if (handleQueuedEdit(key, options)) return;
  if (handlePickerInput(key, options)) return;
  if (handleTokenNavigation(key, options)) return;
  if (handleEscape(key, options)) return;
  if (handleHistoryNavigation(key, options)) return;
  cyclePanel(key, options);
}

export function useShellInput(options: ShellInputOptions): void {
  useInput((input, key) => {
    routeInput(input, key, options);
  });
}
