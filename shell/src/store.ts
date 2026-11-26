/**
 * Zustand store — all shell state in one place.
 * Components read via useAppStore(selector), never own useState for app state.
 */

import { createStore } from "zustand/vanilla";
import { applyToolActivity, cancelRunningToolActivity, createToolActivity, type ToolActivity } from "./activity.js";
import type {
  AgentHealth,
  AgentProfile,
  ContextElision,
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
  TurnBudget,
} from "./api.js";
import { CONTEXT_DISPLAY_WINDOW } from "./api.js";
import { createEmptyConversation } from "./conversation.js";
import { HISTORY_LIMIT } from "./history.js";
import type { ModelPickerState } from "./model-picker.js";
import type { QueuedMessage } from "./queue.js";
import { sanitizeTerminalText } from "./sanitize.js";
import { clearTerminal } from "./term.js";
import type { TokenTab } from "./token-stats.js";
import {
  applyStreamEvent,
  closeLatestTurn,
  createSystemEntry,
  createTurnEntry,
  interruptLatestTurn,
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
export type ChatChannel = "main" | "side";

export type ChatChannelState = {
  session: Session | null;
  transcript: TranscriptEntry[];
  turnCount: number;
  toolActivity: ToolActivity;
  turnTokenUsage: TokenUsage;
  tokenUsage: TokenUsage;
  contextUsage: ContextUsage;
  turnBudget: TurnBudget | null;
  lastElision: ContextElision | null;
  busy: boolean;
  compressing: boolean;
  runStartedAt: number | null;
  recoverableRunId: string | null;
  pendingApproval: PendingApproval | null;
  approvalIndex: number;
  approvalResolving: boolean;
  lastToolCallId: string | null;
  queuedMessages: QueuedMessage[];
};

export function createEmptyChannelState(session: Session | null = null): ChatChannelState {
  return {
    session,
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
    turnBudget: null,
    lastElision: null,
    busy: false,
    compressing: false,
    runStartedAt: null,
    recoverableRunId: null,
    pendingApproval: null,
    approvalIndex: 0,
    approvalResolving: false,
    lastToolCallId: null,
    queuedMessages: [],
  };
}

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
  activeChannel: ChatChannel;
  backgroundChannel: ChatChannelState | null;
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
  turnBudget: TurnBudget | null;
  lastElision: ContextElision | null;
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
  /** Tool call whose result may settle the pending approval prompt. */
  lastToolCallId: string | null;
  modelPicker: ModelPickerState | null;
  inputValue: string;
  inputHistory: string[];
  historyIndex: number;
  historyDraft: string;
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
  interruptTurn: (outcome: "cancelled" | "error") => void;
  getChannelState: (channel: ChatChannel) => ChatChannelState | null;
  updateChannel: (channel: ChatChannel, partial: Partial<ChatChannelState>) => void;
  pushTurnForChannel: (channel: ChatChannel, userText: string) => void;
  pushSystemLineForChannel: (channel: ChatChannel, text: string, tone?: "info" | "error") => void;
  applyEventForChannel: (channel: ChatChannel, event: StreamEvent) => void;
  closeTurnForChannel: (channel: ChatChannel) => void;
  interruptTurnForChannel: (channel: ChatChannel, outcome: "cancelled" | "error") => void;
  switchChannel: (target: ChatChannel, initialSide?: ChatChannelState) => void;
  promoteSideToMain: (promotedSession: Session) => void;
  closeSideChannel: () => void;
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

function applyBoundedStreamEvent(state: AppState, event: StreamEvent): Partial<AppState> {
  return boundedTranscript(state, applyStreamEvent(state.transcript, event));
}

/**
 * Close out an approval once its tool reports back.
 *
 * On denial or the server's 300s timeout the engine emits a tool_result and
 * nothing else, so without this the prompt lingered as a zombie whose Enter
 * 404'd and whose Esc killed the whole run. Approvals are strictly serial — the
 * engine blocks in broker.wait — so the next tool_result is always this call's.
 *
 * Written as a pass over the computed update rather than another branch inside
 * applyStreamState, which is already at its complexity budget.
 */
function clearApprovalOnToolResult(state: AppState, event: StreamEvent, next: Partial<AppState>): Partial<AppState> {
  if (event.type !== "tool_result" || !state.pendingApproval) return next;

  // Only the tool that the pending approval is FOR may settle the prompt.  A
  // stray or duplicate result (SSE retry, gate-blocked, a different overlapping
  // stream) must not silently discard the user's Allow/Deny.  When no tool_call
  // has been seen for this run (lastToolCallId null), fall back to clearing.
  if (state.lastToolCallId !== null && event.id && event.id !== state.lastToolCallId) {
    return next;
  }

  return {
    ...next,
    pendingApproval: null,
    approvalResolving: false,
    statusLine: "",
    transcript: removeApprovalNotice(next.transcript ?? state.transcript, state.pendingApproval.approvalId),
  };
}

function applyDoneStreamState(state: AppState, event: Extract<StreamEvent, { type: "done" }>): Partial<AppState> {
  const bounded = applyBoundedStreamEvent(state, event);
  const transcript = bounded.transcript ?? state.transcript;
  return {
    ...bounded,
    ...(event.budget ? { turnBudget: event.budget } : {}),
    pendingApproval: null,
    approvalResolving: false,
    lastToolCallId: null,
    recoverableRunId: event.status === "completed" ? null : (event.runId ?? state.recoverableRunId),
    // A tool whose result never arrived would otherwise stay in the running
    // map for the rest of the session, leaving the HUD spinner on for every
    // later turn. The transcript converges the same blocks on done.
    toolActivity: cancelRunningToolActivity(applyToolActivity(state.toolActivity, event)),
    transcript: state.pendingApproval ? removeApprovalNotice(transcript, state.pendingApproval.approvalId) : transcript,
  };
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

  if (event.type === "budget_update") {
    const { type: _ignored, ...budget } = event;
    return {
      turnBudget: budget,
    };
  }

  if (event.type === "context_actions_elided") {
    const { type: _ignored, ...elision } = event;
    return {
      lastElision: elision,
      ...applyBoundedStreamEvent(state, event),
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

  if (event.type === "done") return applyDoneStreamState(state, event);

  if (event.type === "run_started") {
    return {
      recoverableRunId: event.runId || state.recoverableRunId,
      // A new run invalidates the previous run's tool-call id for approval
      // settlement matching.
      lastToolCallId: null,
      toolActivity: applyToolActivity(state.toolActivity, event),
      ...applyBoundedStreamEvent(state, event),
    };
  }

  if (event.type === "tool_call") {
    return {
      lastToolCallId: event.id,
      toolActivity: applyToolActivity(state.toolActivity, event),
      ...applyBoundedStreamEvent(state, event),
    };
  }

  if (event.type === "approval_required") {
    return applyApprovalRequired(state, event);
  }

  return {
    toolActivity: applyToolActivity(state.toolActivity, event),
    ...applyBoundedStreamEvent(state, event),
  };
}

function applyApprovalRequired(
  state: AppState,
  event: Extract<StreamEvent, { type: "approval_required" }>,
): Partial<AppState> {
  // A stale approval_required (resumed stream, replayed buffer) must never
  // display run A's command while the user's decision resolves run B: only
  // accept it when it names the run currently being streamed.
  if (state.recoverableRunId && event.runId && event.runId !== state.recoverableRunId) {
    return {
      toolActivity: applyToolActivity(state.toolActivity, event),
      ...applyBoundedStreamEvent(state, event),
    };
  }
  // A duplicate re-emission of the SAME approval while a resolve POST is in
  // flight must not drop the in-flight lock (which would let a second Enter
  // race the first).
  const duplicateWhileResolving = state.approvalResolving && state.pendingApproval?.approvalId === event.approvalId;
  return {
    pendingApproval: event,
    approvalIndex: 0,
    approvalResolving: duplicateWhileResolving ? state.approvalResolving : false,
    // The full-screen tokens/runs panels replace the footer that renders the
    // approval prompt, so leaving the panel up would ask the user to approve
    // a tool call they cannot see.  Return to chat and let them read it.
    ...(state.panel === "tokens" || state.panel === "runs" ? { panel: "chat" as const } : {}),
    statusLine: "Approval required. Review the request and choose Allow or Deny.",
    toolActivity: applyToolActivity(state.toolActivity, event),
    ...applyBoundedStreamEvent(state, event),
  };
}

function snapshotActiveChannel(state: AppState): ChatChannelState {
  return {
    session: state.currentSession,
    transcript: state.transcript,
    turnCount: state.turnCount,
    toolActivity: state.toolActivity,
    turnTokenUsage: state.turnTokenUsage,
    tokenUsage: state.tokenUsage,
    contextUsage: state.contextUsage,
    turnBudget: state.turnBudget,
    lastElision: state.lastElision,
    busy: state.busy,
    compressing: state.compressing,
    runStartedAt: state.runStartedAt,
    recoverableRunId: state.recoverableRunId,
    pendingApproval: state.pendingApproval,
    approvalIndex: state.approvalIndex,
    approvalResolving: state.approvalResolving,
    lastToolCallId: state.lastToolCallId,
    queuedMessages: state.queuedMessages,
  };
}

function projectChannelToState(channel: ChatChannelState): Partial<AppState> {
  return {
    currentSession: channel.session,
    transcript: channel.transcript,
    turnCount: channel.turnCount,
    toolActivity: channel.toolActivity,
    turnTokenUsage: channel.turnTokenUsage,
    tokenUsage: channel.tokenUsage,
    contextUsage: channel.contextUsage,
    turnBudget: channel.turnBudget,
    lastElision: channel.lastElision,
    busy: channel.busy,
    compressing: channel.compressing,
    runStartedAt: channel.runStartedAt,
    recoverableRunId: channel.recoverableRunId,
    pendingApproval: channel.pendingApproval,
    approvalIndex: channel.approvalIndex,
    approvalResolving: channel.approvalResolving,
    lastToolCallId: channel.lastToolCallId,
    queuedMessages: channel.queuedMessages,
  };
}

function applyBackgroundStreamEvent(channel: ChatChannelState, event: StreamEvent): ChatChannelState {
  // Reuse the exact same state machine against a synthetic AppState view of the background channel.
  const synthetic = {
    ...projectChannelToState(channel),
    panel: "chat" as Panel,
    transcriptEpoch: 0,
    statusLine: "",
  } as AppState;
  const nextPartial = clearApprovalOnToolResult(synthetic, event, applyStreamState(synthetic, event));
  const mergedTranscript =
    nextPartial.transcript !== undefined
      ? limitTranscript(nextPartial.transcript, TRANSCRIPT_LIMIT)
      : channel.transcript;
  return {
    ...channel,
    transcript: mergedTranscript,
    toolActivity: nextPartial.toolActivity ?? channel.toolActivity,
    turnTokenUsage: nextPartial.turnTokenUsage ?? channel.turnTokenUsage,
    tokenUsage: nextPartial.tokenUsage ?? channel.tokenUsage,
    contextUsage: nextPartial.contextUsage ?? channel.contextUsage,
    turnBudget: "turnBudget" in nextPartial ? (nextPartial.turnBudget ?? null) : channel.turnBudget,
    lastElision: "lastElision" in nextPartial ? (nextPartial.lastElision ?? null) : channel.lastElision,
    compressing: nextPartial.compressing ?? channel.compressing,
    recoverableRunId:
      "recoverableRunId" in nextPartial ? (nextPartial.recoverableRunId ?? null) : channel.recoverableRunId,
    pendingApproval:
      "pendingApproval" in nextPartial ? (nextPartial.pendingApproval ?? null) : channel.pendingApproval,
    approvalIndex: nextPartial.approvalIndex ?? channel.approvalIndex,
    approvalResolving: nextPartial.approvalResolving ?? channel.approvalResolving,
    lastToolCallId:
      "lastToolCallId" in nextPartial ? (nextPartial.lastToolCallId ?? null) : channel.lastToolCallId,
  };
}

export function createAppStore(initialHistory: string[] = []) {
  return createStore<AppStore>((set, get) => ({
    mode: "boot",
    panel: "welcome",
    activeChannel: "main",
    backgroundChannel: null,
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
    turnBudget: null,
    lastElision: null,
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
    lastToolCallId: null,
    modelPicker: null,
    inputValue: "",
    inputHistory: initialHistory,
    historyIndex: -1,
    historyDraft: "",
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

    set: (partial) => {
      if (partial && "statusLine" in partial && typeof partial.statusLine === "string") {
        // statusLine embeds server-derived paths, MCP error text, and model
        // names; sanitise it at the single store boundary so no caller has to
        // remember.
        set({ ...partial, statusLine: sanitizeTerminalText(partial.statusLine) });
      } else {
        set(partial);
      }
    },

    pushHistory: (text) =>
      set((s) => ({
        inputHistory:
          s.inputHistory[s.inputHistory.length - 1] === text
            ? s.inputHistory
            : [...s.inputHistory, text].slice(-HISTORY_LIMIT),
        historyIndex: -1,
        historyDraft: "",
      })),
    pushSystemLine: (text, tone = "info") =>
      set((s) => appendTranscript(s, createSystemEntry(sanitizeTerminalText(text), tone))),
    pushTurn: (userText) =>
      set((s) => ({
        ...appendTranscript(s, createTurnEntry(userText)),
        turnCount: s.turnCount + 1,
        turnTokenUsage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
        turnBudget: null,
        lastElision: null,
        toolActivity: { ...s.toolActivity, calls: {}, running: {} },
      })),
    applyEvent: (event) => set((state) => clearApprovalOnToolResult(state, event, applyStreamState(state, event))),
    closeTurn: () => set((s) => ({ transcript: closeLatestTurn(s.transcript) })),
    interruptTurn: (outcome) =>
      set((s) => ({
        toolActivity: cancelRunningToolActivity(s.toolActivity),
        transcript: interruptLatestTurn(s.transcript, outcome),
      })),

    getChannelState: (channel) => {
      const s = get();
      if (channel === s.activeChannel) return snapshotActiveChannel(s);
      return s.backgroundChannel;
    },

    updateChannel: (channel, partial) =>
      set((s) => {
        if (channel === s.activeChannel) {
          const mapped: Partial<AppState> = {};
          if ("session" in partial) mapped.currentSession = partial.session ?? null;
          if ("transcript" in partial && partial.transcript) mapped.transcript = partial.transcript;
          if ("turnCount" in partial && partial.turnCount !== undefined) mapped.turnCount = partial.turnCount;
          if ("toolActivity" in partial && partial.toolActivity) mapped.toolActivity = partial.toolActivity;
          if ("turnTokenUsage" in partial && partial.turnTokenUsage) mapped.turnTokenUsage = partial.turnTokenUsage;
          if ("tokenUsage" in partial && partial.tokenUsage) mapped.tokenUsage = partial.tokenUsage;
          if ("contextUsage" in partial && partial.contextUsage) mapped.contextUsage = partial.contextUsage;
          if ("turnBudget" in partial) mapped.turnBudget = partial.turnBudget ?? null;
          if ("lastElision" in partial) mapped.lastElision = partial.lastElision ?? null;
          if ("busy" in partial && partial.busy !== undefined) mapped.busy = partial.busy;
          if ("compressing" in partial && partial.compressing !== undefined) mapped.compressing = partial.compressing;
          if ("runStartedAt" in partial) mapped.runStartedAt = partial.runStartedAt ?? null;
          if ("recoverableRunId" in partial) mapped.recoverableRunId = partial.recoverableRunId ?? null;
          if ("pendingApproval" in partial) mapped.pendingApproval = partial.pendingApproval ?? null;
          if ("approvalIndex" in partial && partial.approvalIndex !== undefined) mapped.approvalIndex = partial.approvalIndex;
          if ("approvalResolving" in partial && partial.approvalResolving !== undefined) mapped.approvalResolving = partial.approvalResolving;
          if ("lastToolCallId" in partial) mapped.lastToolCallId = partial.lastToolCallId ?? null;
          if ("queuedMessages" in partial && partial.queuedMessages) mapped.queuedMessages = partial.queuedMessages;
          return mapped;
        }
        if (!s.backgroundChannel) return {};
        return { backgroundChannel: { ...s.backgroundChannel, ...partial } };
      }),

    pushTurnForChannel: (channel, userText) =>
      set((s) => {
        if (channel === s.activeChannel) {
          return {
            ...appendTranscript(s, createTurnEntry(userText)),
            turnCount: s.turnCount + 1,
            turnTokenUsage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
            turnBudget: null,
            lastElision: null,
            toolActivity: { ...s.toolActivity, calls: {}, running: {} },
          };
        }
        if (!s.backgroundChannel) return {};
        const bg = s.backgroundChannel;
        return {
          backgroundChannel: {
            ...bg,
            transcript: limitTranscript([...bg.transcript, createTurnEntry(userText)], TRANSCRIPT_LIMIT),
            turnCount: bg.turnCount + 1,
            turnTokenUsage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
            turnBudget: null,
            lastElision: null,
            toolActivity: { ...bg.toolActivity, calls: {}, running: {} },
          },
        };
      }),

    pushSystemLineForChannel: (channel, text, tone = "info") =>
      set((s) => {
        const safe = sanitizeTerminalText(text);
        if (channel === s.activeChannel) {
          return appendTranscript(s, createSystemEntry(safe, tone));
        }
        if (!s.backgroundChannel) return {};
        return {
          backgroundChannel: {
            ...s.backgroundChannel,
            transcript: limitTranscript([...s.backgroundChannel.transcript, createSystemEntry(safe, tone)], TRANSCRIPT_LIMIT),
          },
        };
      }),

    applyEventForChannel: (channel, event) =>
      set((s) => {
        if (channel === s.activeChannel) {
          return clearApprovalOnToolResult(s, event, applyStreamState(s, event));
        }
        if (!s.backgroundChannel) return {};
        return { backgroundChannel: applyBackgroundStreamEvent(s.backgroundChannel, event) };
      }),

    closeTurnForChannel: (channel) =>
      set((s) => {
        if (channel === s.activeChannel) {
          return { transcript: closeLatestTurn(s.transcript) };
        }
        if (!s.backgroundChannel) return {};
        return {
          backgroundChannel: {
            ...s.backgroundChannel,
            transcript: closeLatestTurn(s.backgroundChannel.transcript),
          },
        };
      }),

    interruptTurnForChannel: (channel, outcome) =>
      set((s) => {
        if (channel === s.activeChannel) {
          return {
            toolActivity: cancelRunningToolActivity(s.toolActivity),
            transcript: interruptLatestTurn(s.transcript, outcome),
          };
        }
        if (!s.backgroundChannel) return {};
        return {
          backgroundChannel: {
            ...s.backgroundChannel,
            toolActivity: cancelRunningToolActivity(s.backgroundChannel.toolActivity),
            transcript: interruptLatestTurn(s.backgroundChannel.transcript, outcome),
          },
        };
      }),

    switchChannel: (target, initialSide) =>
      set((s) => {
        if (target === s.activeChannel) return {};
        const currentSnapshot = snapshotActiveChannel(s);
        const nextActive =
          target === "side"
            ? (initialSide ?? s.backgroundChannel ?? createEmptyChannelState())
            : (s.backgroundChannel ?? createEmptyChannelState());
        return {
          ...projectChannelToState(nextActive),
          activeChannel: target,
          backgroundChannel: currentSnapshot,
          panel: "chat",
          transcriptEpoch: s.transcriptEpoch + 1,
          statusLine:
            target === "side"
              ? "Side chat (read-only). Type /back or Ctrl+\\ to return, /promote to make permanent."
              : "Returned to main conversation.",
        };
      }),

    promoteSideToMain: (promotedSession) =>
      set((s) => {
        if (s.activeChannel === "side") {
          return {
            currentSession: promotedSession,
            activeChannel: "main",
            backgroundChannel: null,
            panel: "chat",
            transcriptEpoch: s.transcriptEpoch + 1,
            statusLine: `Promoted side chat to main session (${promotedSession.id}).`,
          };
        }
        if (s.backgroundChannel) {
          return {
            ...projectChannelToState({ ...s.backgroundChannel, session: promotedSession }),
            activeChannel: "main",
            backgroundChannel: null,
            panel: "chat",
            transcriptEpoch: s.transcriptEpoch + 1,
            statusLine: `Promoted side chat to main session (${promotedSession.id}).`,
          };
        }
        return {};
      }),

    closeSideChannel: () =>
      set((s) => {
        if (s.activeChannel === "side") {
          const mainState = s.backgroundChannel ?? createEmptyChannelState();
          return {
            ...projectChannelToState(mainState),
            activeChannel: "main",
            backgroundChannel: null,
            panel: mainState.transcript.length > 0 ? "chat" : "welcome",
            transcriptEpoch: s.transcriptEpoch + 1,
            statusLine: "Side chat closed.",
          };
        }
        return { backgroundChannel: null, statusLine: "Side chat closed." };
      }),

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
