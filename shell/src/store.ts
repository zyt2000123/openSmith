/**
 * Zustand store — all shell state in one place.
 * Components read via useAppStore(selector), never own useState for app state.
 */

import { createStore } from "zustand/vanilla";
import { applyToolActivity, createToolActivity, type ToolActivity } from "./activity.js";
import type {
  AgentHealth,
  AgentProfile,
  ContextUsage,
  LlmConfig,
  McpServer,
  ObservabilityRun,
  PendingApproval,
  RunIncident,
  Session,
  SkillSummary,
  StreamEvent,
  TokenStats,
  TokenUsage,
} from "./api.js";
import { CONTEXT_DISPLAY_WINDOW } from "./api.js";
import { createEmptyConversation } from "./conversation.js";
import { HISTORY_LIMIT } from "./history.js";
import type { ModelPickerState } from "./model-picker.js";
import type { QueuedMessage } from "./queue.js";
import { clearTerminal } from "./term.js";
import type { TokenTab } from "./token-stats.js";
import {
  applyStreamEvent,
  closeLatestTurn,
  createSystemEntry,
  createTurnEntry,
  limitTranscript,
  removeApprovalNotice,
  type TranscriptEntry,
  type TranscriptViewMode,
} from "./transcript-state.js";

export type Panel =
  | "welcome"
  | "chat"
  | "sessions"
  | "skill-actions"
  | "skills"
  | "skill-toggle"
  | "mcp"
  | "hooks"
  | "hook-details"
  | "tokens"
  | "runs";
export type Mode = "boot" | "setup" | "chat";
export type SetupFlow = "initial" | "advanced";

export type SetupDraft = {
  /** Human-readable company or relay name; it does not affect the wire protocol. */
  vendor: string;
  provider: string;
  base_url: string;
  api_key: string;
  model: string;
  review_model: string;
  max_output_tokens: string;
  routes: string;
  models: string;
  interactive_api_key: string;
  gate_api_key: string;
  background_api_key: string;
  timeout_profiles: string;
};

export type AppState = {
  mode: Mode;
  panel: Panel;
  baseUrl: string;
  config: LlmConfig | null;
  agent: AgentProfile | null;
  sessions: Session[];
  skills: SkillSummary[];
  mcpServers: McpServer[];
  currentSession: Session | null;
  selectedModelProfile: string | null;
  transcript: TranscriptEntry[];
  /** Bumped whenever the transcript is replaced wholesale — remounts <Static>. */
  transcriptEpoch: number;
  turnCount: number;
  toolActivity: ToolActivity;
  /** Token usage accumulated across the current user message and its agent work. */
  turnTokenUsage: TokenUsage;
  tokenUsage: TokenUsage;
  contextUsage: ContextUsage;
  tokenStats: TokenStats | null;
  tokenTab: TokenTab;
  observabilityRuns: ObservabilityRun[] | null;
  observabilityHealth: AgentHealth | null;
  observabilityIncidents: RunIncident[] | null;
  viewMode: TranscriptViewMode;
  pendingSkill: SkillSummary | null;
  queuedMessages: QueuedMessage[];
  inputLocked: boolean;
  busy: boolean;
  compressing: boolean;
  runStartedAt: number | null;
  /** Last non-completed run, retained so a disconnected Shell can resume it. */
  recoverableRunId: string | null;
  pendingApproval: PendingApproval | null;
  approvalIndex: number;
  approvalResolving: boolean;
  modelPicker: ModelPickerState | null;
  inputValue: string;
  inputHistory: string[];
  historyIndex: number;
  statusLine: string;
  setupDraft: SetupDraft;
  setupFlow: SetupFlow;
  setupIndex: number;
  slashIndex: number;
  skillsIndex: number;
  skillActionIndex: number;
  hooksIndex: number;
  skillMentionIndex: number;
  welcomeNotice: { text: string; tone: "info" | "error" } | null;
};

export type AppActions = {
  set: (partial: Partial<AppState>) => void;
  pushSystemLine: (text: string, tone?: "info" | "error") => void;
  pushHistory: (text: string) => void;
  pushTurn: (userText: string) => void;
  applyEvent: (event: StreamEvent) => void;
  closeTurn: () => void;
  resetChat: () => void;
  clearChat: () => void;
  startFreshSession: () => void;
  hydrate: (opts: {
    agent: AgentProfile;
    sessions: Session[];
    skills: SkillSummary[];
    mcpServers: McpServer[];
    config: LlmConfig;
    notices?: string[];
  }) => void;
};

export type AppStore = AppState & AppActions;

export const TRANSCRIPT_LIMIT = 200;
/** How far a trim cuts back, so repaints are occasional instead of per-message. */
export const TRANSCRIPT_TRIM_TARGET = 150;

type HydrateOptions = {
  agent: AgentProfile;
  sessions: Session[];
  skills: SkillSummary[];
  mcpServers: McpServer[];
  config: LlmConfig;
  notices?: string[];
};

function hydrateShellState(state: AppState, options: HydrateOptions): Partial<AppState> {
  const notices = options.notices ?? [];
  const hasWarnings = notices.some((notice) => notice.includes("unavailable") || notice.includes("could not"));
  return {
    agent: options.agent,
    sessions: options.sessions,
    skills: options.skills,
    mcpServers: options.mcpServers,
    config: options.config,
    mode: "chat",
    panel: state.transcript.length > 0 ? "chat" : "welcome",
    inputValue: "",
    welcomeNotice: notices.length > 0 ? { text: notices.join("\n"), tone: hasWarnings ? "error" : "info" } : null,
    statusLine: hasWarnings
      ? "Ready, with warnings. Type / for commands."
      : "Ready. Type / for commands or @ for skills.",
  };
}

/**
 * Ink's <Static> assumes its item list only ever grows: it prints `items.slice(index)`
 * and never lowers `index`. Dropping the oldest entries therefore strands every later
 * append — the terminal would silently stop showing new output. A truncating write has
 * to wipe the screen and bump the epoch that remounts <Static>, exactly like /clear.
 */
function boundedTranscript(state: AppState, transcript: TranscriptEntry[]): Partial<AppState> {
  if (transcript.length <= TRANSCRIPT_LIMIT) return { transcript };

  // Trim in batches, not one-in-one-out: each trim costs a full repaint, so
  // shedding a quarter buys TRANSCRIPT_LIMIT/4 quiet appends before the next one.
  clearTerminal();
  return {
    transcript: limitTranscript(transcript, TRANSCRIPT_TRIM_TARGET),
    transcriptEpoch: state.transcriptEpoch + 1,
  };
}

function appendTranscript(state: AppState, entry: TranscriptEntry): Partial<AppState> {
  return boundedTranscript(state, [...state.transcript, entry]);
}

function applyStreamState(state: AppState, event: StreamEvent): Partial<AppState> {
  if (event.type === "token_usage") {
    return {
      toolActivity: applyToolActivity(state.toolActivity, event),
      turnTokenUsage: {
        input_tokens: state.turnTokenUsage.input_tokens + event.input_tokens,
        output_tokens: state.turnTokenUsage.output_tokens + event.output_tokens,
        total_tokens: state.turnTokenUsage.total_tokens + event.total_tokens,
      },
      tokenUsage: {
        input_tokens: state.tokenUsage.input_tokens + event.input_tokens,
        output_tokens: state.tokenUsage.output_tokens + event.output_tokens,
        total_tokens: state.tokenUsage.total_tokens + event.total_tokens,
      },
    };
  }

  if (event.type === "context_usage") {
    return {
      contextUsage: event,
      toolActivity: applyToolActivity(state.toolActivity, event),
      transcript: applyStreamEvent(state.transcript, event),
    };
  }

  if (event.type === "compression") {
    return {
      compressing: event.active,
      toolActivity: applyToolActivity(state.toolActivity, event),
      transcript: applyStreamEvent(state.transcript, event),
    };
  }

  if (event.type === "done") {
    const transcript = applyStreamEvent(state.transcript, event);
    return {
      pendingApproval: null,
      approvalResolving: false,
      recoverableRunId: event.status === "completed" ? null : (event.runId ?? state.recoverableRunId),
      toolActivity: applyToolActivity(state.toolActivity, event),
      transcript: state.pendingApproval
        ? removeApprovalNotice(transcript, state.pendingApproval.approvalId)
        : transcript,
    };
  }

  if (event.type === "run_started") {
    return {
      recoverableRunId: event.runId || state.recoverableRunId,
      toolActivity: applyToolActivity(state.toolActivity, event),
      transcript: applyStreamEvent(state.transcript, event),
    };
  }

  if (event.type === "approval_required") {
    return {
      pendingApproval: event,
      approvalIndex: 0,
      approvalResolving: false,
      statusLine: "Approval required. Review the request and choose Allow or Deny.",
      toolActivity: applyToolActivity(state.toolActivity, event),
      // The only stream event that appends an entry, so it needs the same bound
      // as pushTurn/pushSystemLine — every other event edits the turn in place.
      ...boundedTranscript(state, applyStreamEvent(state.transcript, event)),
    };
  }

  return {
    toolActivity: applyToolActivity(state.toolActivity, event),
    transcript: applyStreamEvent(state.transcript, event),
  };
}

export function createAppStore(initialHistory: string[] = []) {
  return createStore<AppStore>((set) => ({
    mode: "boot",
    panel: "welcome",
    baseUrl: "",
    config: null,
    agent: null,
    sessions: [],
    skills: [],
    mcpServers: [],
    currentSession: null,
    selectedModelProfile: null,
    transcript: [],
    transcriptEpoch: 0,
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
    tokenStats: null,
    tokenTab: "stats",
    observabilityRuns: null,
    observabilityHealth: null,
    observabilityIncidents: null,
    viewMode: "compact",
    pendingSkill: null,
    queuedMessages: [],
    inputLocked: false,
    busy: false,
    compressing: false,
    runStartedAt: null,
    recoverableRunId: null,
    pendingApproval: null,
    approvalIndex: 0,
    approvalResolving: false,
    modelPicker: null,
    inputValue: "",
    inputHistory: initialHistory,
    historyIndex: -1,
    statusLine: "Booting Smith…",
    setupDraft: {
      vendor: "",
      provider: "openai",
      base_url: "https://api.openai.com/v1",
      api_key: "",
      model: "gpt-4.1-mini",
      review_model: "",
      max_output_tokens: "",
      routes: "",
      models: "",
      interactive_api_key: "",
      gate_api_key: "",
      background_api_key: "",
      timeout_profiles: "",
    },
    setupFlow: "initial",
    setupIndex: 0,
    slashIndex: 0,
    skillsIndex: 0,
    skillActionIndex: 0,
    hooksIndex: 0,
    skillMentionIndex: 0,
    welcomeNotice: null,

    set: (partial) => set(partial),

    pushHistory: (text) =>
      set((s) => ({
        inputHistory:
          s.inputHistory[s.inputHistory.length - 1] === text
            ? s.inputHistory
            : [...s.inputHistory, text].slice(-HISTORY_LIMIT),
        historyIndex: -1,
      })),
    pushSystemLine: (text, tone = "info") => set((s) => appendTranscript(s, createSystemEntry(text, tone))),
    pushTurn: (userText) =>
      set((s) => ({
        ...appendTranscript(s, createTurnEntry(userText)),
        turnCount: s.turnCount + 1,
        turnTokenUsage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
      })),
    applyEvent: (event) => set((state) => applyStreamState(state, event)),
    closeTurn: () => set((s) => ({ transcript: closeLatestTurn(s.transcript) })),

    resetChat: () =>
      set((s) => ({
        ...createEmptyConversation("welcome", "Fresh shell ready."),
        pendingApproval: null,
        approvalIndex: 0,
        approvalResolving: false,
        modelPicker: null,
        transcriptEpoch: s.transcriptEpoch + 1,
      })),
    clearChat: () =>
      set((s) => ({
        ...createEmptyConversation("chat", "Conversation cleared. Next message starts a fresh session."),
        pendingApproval: null,
        approvalIndex: 0,
        approvalResolving: false,
        modelPicker: null,
        transcriptEpoch: s.transcriptEpoch + 1,
      })),
    startFreshSession: () =>
      set((s) => ({
        ...createEmptyConversation("chat", "Fresh session ready."),
        pendingApproval: null,
        approvalIndex: 0,
        approvalResolving: false,
        modelPicker: null,
        transcriptEpoch: s.transcriptEpoch + 1,
      })),

    hydrate: (options) => {
      // 不清空 transcript/currentSession：mid-session /config 保存也走这里，进行中的会话要保留
      set((state) => hydrateShellState(state, options));
    },
  }));
}
