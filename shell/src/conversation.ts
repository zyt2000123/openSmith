import { createToolActivity, type ToolActivity } from "./activity.js";
import { CONTEXT_DISPLAY_WINDOW, type ContextUsage, type Session, type SkillSummary, type TokenUsage } from "./api.js";
import type { QueuedMessage } from "./queue.js";
import type { TranscriptEntry } from "./transcript-state.js";

export type ConversationPanel = "welcome" | "chat";

export type EmptyConversation = {
  currentSession: Session | null;
  transcript: TranscriptEntry[];
  turnCount: number;
  toolActivity: ToolActivity;
  turnTokenUsage: TokenUsage;
  tokenUsage: TokenUsage;
  contextUsage: ContextUsage;
  pendingSkill: SkillSummary | null;
  queuedMessages: QueuedMessage[];
  inputLocked: boolean;
  busy: boolean;
  compressing: boolean;
  runStartedAt: number | null;
  recoverableRunId: string | null;
  historyIndex: number;
  historyDraft: string;
  slashIndex: number;
  skillsIndex: number;
  skillActionIndex: number;
  hooksIndex: number;
  welcomeNotice: { text: string; tone: "info" | "error" } | null;
  panel: ConversationPanel;
  statusLine: string;
};

export function createEmptyConversation(panel: ConversationPanel, statusLine: string): EmptyConversation {
  return {
    currentSession: null,
    transcript: [],
    turnCount: 0,
    toolActivity: createToolActivity(),
    turnTokenUsage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
    tokenUsage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
    contextUsage: {
      context_tokens: 0,
      context_window: CONTEXT_DISPLAY_WINDOW,
      context_percent: 0,
      estimated: true,
    },
    pendingSkill: null,
    queuedMessages: [],
    inputLocked: false,
    busy: false,
    compressing: false,
    runStartedAt: null,
    recoverableRunId: null,
    historyIndex: -1,
    historyDraft: "",
    slashIndex: 0,
    skillsIndex: 0,
    skillActionIndex: 0,
    hooksIndex: 0,
    welcomeNotice: null,
    panel,
    statusLine,
  };
}
