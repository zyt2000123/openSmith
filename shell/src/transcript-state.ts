/**
 * Transcript state machine — pure functions only, no rendering.
 * transcript.tsx renders these structures; store.ts mutates them via applyStreamEvent.
 */

import { type ToolState, toolStateFromResult } from "./activity.js";
import type { Message, StreamEvent } from "./api.js";
import type { SmithUiPayload } from "./smith-ui-schema.js";

type SystemTone = "info" | "error";
export type SkillState = "running" | "retry" | "waiting" | "done" | "blocked" | "error" | "cancelled";
export type TranscriptViewMode = "compact" | "transcript";

export function limitTranscript(entries: TranscriptEntry[], limit: number): TranscriptEntry[] {
  return entries.length <= limit ? entries : entries.slice(-limit);
}

export type SystemEntry = {
  id: string;
  kind: "system";
  text: string;
  tone: SystemTone;
  approvalId?: string;
};

export type ThinkingBlock = {
  id: string;
  type: "thinking";
  text: string;
  done: boolean;
};

export type ToolBlock = {
  id: string;
  type: "tool";
  toolCallId: string;
  name: string;
  hint: string;
  state: ToolState;
  summary: string;
};

export type SkillBlock = {
  id: string;
  type: "skill";
  name: string;
  state: SkillState;
  activities: SkillActivity[];
};

export type SkillActivity = {
  id: string;
  name: string;
  hint: string;
  state: ToolState;
  summary: string;
};

export type SmithUiBlock = {
  id: string;
  type: "smith_ui";
  payload: SmithUiPayload;
};

export type SmithUiFallbackBlock = {
  id: string;
  type: "smith_ui_fallback";
  reason: string;
  code: string;
};

export type ProvisionalText = {
  provisionId: string;
  text: string;
};

export type TurnBlock = ThinkingBlock | ToolBlock | SkillBlock | SmithUiBlock | SmithUiFallbackBlock;

export type TurnEntry = {
  id: string;
  kind: "turn";
  userText: string;
  assistantText: string;
  provisional: ProvisionalText[];
  blocks: TurnBlock[];
  streaming: boolean;
};

export type TranscriptEntry = SystemEntry | TurnEntry;

function createId(): string {
  return Math.random().toString(36).slice(2, 10);
}

function findLastTurnIndex(entries: TranscriptEntry[]): number {
  for (let i = entries.length - 1; i >= 0; i -= 1) {
    if (entries[i]?.kind === "turn") {
      return i;
    }
  }
  return -1;
}

function finishThinkingBlocks(blocks: TurnBlock[]): TurnBlock[] {
  const last = blocks[blocks.length - 1];
  if (last?.type !== "thinking" || last.done) {
    return blocks;
  }

  if (!last.text.trim()) {
    return blocks.slice(0, -1);
  }

  return [...blocks.slice(0, -1), { ...last, done: true }];
}

function appendProvisionalText(provisional: ProvisionalText[], provisionId: string, text: string): ProvisionalText[] {
  const index = provisional.findIndex((item) => item.provisionId === provisionId);
  if (index === -1) {
    return [...provisional, { provisionId, text }];
  }

  const existing = provisional[index];
  if (!existing) return provisional;
  const next = [...provisional];
  next[index] = { ...existing, text: existing.text + text };
  return next;
}

/** Concatenate a turn's visible drafts, matching what the server persists. */
function provisionalToText(provisional: ProvisionalText[]): string {
  return provisional.map((item) => item.text).join("");
}

function updateLastTurn(entries: TranscriptEntry[], updater: (turn: TurnEntry) => TurnEntry): TranscriptEntry[] {
  const index = findLastTurnIndex(entries);
  if (index === -1) {
    return entries;
  }

  const turn = entries[index];
  if (turn?.kind !== "turn") {
    return entries;
  }

  const nextTurn = updater(turn);
  if (nextTurn === turn) {
    return entries;
  }

  return [...entries.slice(0, index), nextTurn, ...entries.slice(index + 1)];
}

function nextSkillState(status: string): SkillState {
  if (status === "start") return "running";
  if (status === "retry") return "retry";
  if (status === "blocked") return "blocked";
  if (status === "error" || status === "incomplete") return "error";
  return "done";
}

// System notices for route / gate / backtrack / awaiting_input used to be
// pushed into the transcript here, but the workflow step card already
// reflects running / retry / waiting state, and these notices were just
// internal pipeline telemetry that polluted the chat. Keep them in the
// skill block's `state` field; drop the rendered copy.

function markLatestTurnWaitingForInput(entries: TranscriptEntry[]): TranscriptEntry[] {
  return updateLastTurn(entries, (turn) => ({
    ...turn,
    blocks: finishThinkingBlocks(turn.blocks).map((block) =>
      block.type === "skill" && (block.state === "running" || block.state === "retry")
        ? { ...block, state: "waiting" }
        : block,
    ),
    streaming: false,
  }));
}

function updateSkillActivity(
  blocks: TurnBlock[],
  toolCallId: string,
  updater: (activity: SkillActivity) => SkillActivity,
): TurnBlock[] | null {
  for (let blockIndex = blocks.length - 1; blockIndex >= 0; blockIndex -= 1) {
    const block = blocks[blockIndex];
    if (block?.type !== "skill") continue;

    const activityIndex = block.activities.findIndex((activity) => activity.id === toolCallId);
    if (activityIndex === -1) continue;

    const activity = block.activities[activityIndex];
    if (!activity) return blocks;
    const activities = [...block.activities];
    activities[activityIndex] = updater(activity);
    const next = [...blocks];
    next[blockIndex] = { ...block, activities };
    return next;
  }
  return null;
}

function activeSkillIndex(blocks: TurnBlock[]): number {
  return [...blocks].reverse().findIndex((block) => block.type === "skill" && block.state === "running");
}

type ToolCallEvent = Extract<StreamEvent, { type: "tool_call" }>;
type ToolResultEvent = Extract<StreamEvent, { type: "tool_result" }>;

function appendToolActivityToActiveSkill(blocks: TurnBlock[], event: ToolCallEvent): TurnBlock[] | null {
  const reversedSkillIndex = activeSkillIndex(blocks);
  if (reversedSkillIndex < 0) return null;

  const skillIndex = blocks.length - 1 - reversedSkillIndex;
  const skill = blocks[skillIndex];
  if (skill?.type !== "skill") return null;

  const activityIndex = skill.activities.findIndex((activity) => activity.id === event.id);
  const activities = [...skill.activities];
  if (activityIndex >= 0) {
    const existing = activities[activityIndex];
    if (!existing) return blocks;
    activities[activityIndex] = {
      ...existing,
      name: event.name || existing.name,
      hint: event.hint || existing.hint,
      state: "running",
    };
  } else {
    activities.push({
      id: event.id,
      name: event.name || "tool",
      hint: event.hint || "",
      state: "running",
      summary: "",
    });
  }

  const next = [...blocks];
  next[skillIndex] = { ...skill, activities };
  return next;
}

function applyStandaloneToolCall(blocks: TurnBlock[], event: ToolCallEvent): TurnBlock[] {
  const existingIndex = blocks.findIndex((block) => block.type === "tool" && block.toolCallId === event.id);
  if (existingIndex < 0) {
    return [
      ...blocks,
      {
        id: createId(),
        type: "tool",
        toolCallId: event.id,
        name: event.name || "tool",
        hint: event.hint || "",
        state: "running",
        summary: "",
      },
    ];
  }

  const existing = blocks[existingIndex];
  if (existing?.type !== "tool") return blocks;
  const next = [...blocks];
  next[existingIndex] = {
    ...existing,
    name: event.name || existing.name,
    hint: event.hint || existing.hint,
    state: "running",
  };
  return next;
}

function applyToolCallToTurn(turn: TurnEntry, event: ToolCallEvent): TurnEntry {
  const blocks = finishThinkingBlocks(turn.blocks);
  const skillBlocks = appendToolActivityToActiveSkill(blocks, event);
  return { ...turn, blocks: skillBlocks ?? applyStandaloneToolCall(blocks, event) };
}

function applyStandaloneToolResult(blocks: TurnBlock[], event: ToolResultEvent, state: ToolState): TurnBlock[] {
  const reversedIndex = [...blocks]
    .reverse()
    .findIndex((block) => block.type === "tool" && block.toolCallId === event.id);
  if (reversedIndex < 0) {
    return [
      ...blocks,
      {
        id: createId(),
        type: "tool",
        toolCallId: event.id,
        name: "tool",
        hint: "",
        state,
        summary: event.summary || "",
      },
    ];
  }

  const index = blocks.length - 1 - reversedIndex;
  const existing = blocks[index];
  if (existing?.type !== "tool") return blocks;
  const next = [...blocks];
  next[index] = {
    ...existing,
    state,
    summary: event.summary || existing.summary,
  };
  return next;
}

function applyToolResultToTurn(turn: TurnEntry, event: ToolResultEvent): TurnEntry {
  const blocks = finishThinkingBlocks(turn.blocks);
  const state = toolStateFromResult(event);
  const skillBlocks = updateSkillActivity(blocks, event.id, (activity) => ({
    ...activity,
    state,
    summary: event.summary || activity.summary,
  }));
  return { ...turn, blocks: skillBlocks ?? applyStandaloneToolResult(blocks, event, state) };
}

export function createSystemEntry(text: string, tone: SystemTone = "info"): SystemEntry {
  return {
    id: createId(),
    kind: "system",
    text,
    tone,
  };
}

export function removeApprovalNotice(entries: TranscriptEntry[], approvalId: string): TranscriptEntry[] {
  return entries.filter((entry) => entry.kind !== "system" || entry.approvalId !== approvalId);
}

export function createTurnEntry(userText: string): TurnEntry {
  return {
    id: createId(),
    kind: "turn",
    userText,
    assistantText: "",
    provisional: [],
    blocks: [],
    streaming: true,
  };
}

export function closeLatestTurn(entries: TranscriptEntry[]): TranscriptEntry[] {
  return updateLastTurn(entries, (turn) => ({
    ...turn,
    blocks: finishThinkingBlocks(turn.blocks),
    provisional: [],
    streaming: false,
  }));
}

export function interruptLatestTurn(
  entries: TranscriptEntry[],
  outcome: Extract<ToolState, "cancelled" | "error">,
  fallbackSummary?: string,
): TranscriptEntry[] {
  const fallback =
    fallbackSummary ?? (outcome === "cancelled" ? "Cancelled before completion." : "Request failed before completion.");
  return updateLastTurn(entries, (turn) => ({
    ...turn,
    // The server persists whatever draft was visible as the assistant message.
    // Erasing it here would show the user an empty turn while the stored
    // session — and the next request's history — held text they never saw.
    assistantText: turn.assistantText || provisionalToText(turn.provisional),
    blocks: finishThinkingBlocks(turn.blocks).map((block) => {
      if (block.type === "tool" && block.state === "running") {
        return { ...block, state: outcome, summary: block.summary || fallback };
      }
      if (block.type !== "skill") return block;
      return {
        ...block,
        state: block.state === "running" || block.state === "retry" ? outcome : block.state,
        activities: block.activities.map((activity) =>
          activity.state === "running"
            ? { ...activity, state: outcome, summary: activity.summary || fallback }
            : activity,
        ),
      };
    }),
    provisional: [],
    streaming: false,
  }));
}

/** Clear the incomplete assistant output while retaining the user turn for a run retry. */
export function restartLatestTurn(entries: TranscriptEntry[]): TranscriptEntry[] {
  return updateLastTurn(entries, (turn) => ({
    ...turn,
    assistantText: "",
    provisional: [],
    blocks: [],
    streaming: true,
  }));
}

function approvalArgumentsSummary(arguments_: Record<string, unknown>): string {
  const entries = Object.entries(arguments_);
  if (entries.length === 0) return "";
  const text = entries
    .map(([key, value]) => `${key}=${typeof value === "string" ? value : (JSON.stringify(value) ?? String(value))}`)
    .join(", ");
  const safeText = Array.from(text, (character) => {
    const code = character.charCodeAt(0);
    return code < 32 || code === 127 ? " " : character;
  }).join("");
  return ` Details: ${safeText.slice(0, 500)}.`;
}

export function applyStreamEvent(entries: TranscriptEntry[], event: StreamEvent): TranscriptEntry[] {
  switch (event.type) {
    case "run_started":
      return entries;

    case "approval_required":
      return [
        ...entries,
        {
          ...createSystemEntry(
            `Approval required for ${event.tool} (${event.level}). ${event.reason}${approvalArgumentsSummary(event.arguments)} Use /approve or /deny.`,
            "info",
          ),
          approvalId: event.approvalId,
        },
      ];

    case "provisional_text_delta":
      if (!event.provisionId || !event.text) return entries;
      return updateLastTurn(entries, (turn) => ({
        ...turn,
        blocks: finishThinkingBlocks(turn.blocks),
        provisional: appendProvisionalText(turn.provisional, event.provisionId, event.text),
      }));

    case "provisional_commit":
      if (!event.provisionId) return entries;
      return updateLastTurn(entries, (turn) => {
        const index = turn.provisional.findIndex((item) => item.provisionId === event.provisionId);
        const settled = turn.provisional[index];
        if (!settled) return turn;
        return {
          ...turn,
          assistantText: turn.assistantText + settled.text,
          provisional: [...turn.provisional.slice(0, index), ...turn.provisional.slice(index + 1)],
        };
      });

    case "provisional_retract":
      if (!event.provisionId) return entries;
      return updateLastTurn(entries, (turn) => {
        const provisional = turn.provisional.filter((item) => item.provisionId !== event.provisionId);
        return provisional.length === turn.provisional.length ? turn : { ...turn, provisional };
      });

    case "message":
      return updateLastTurn(entries, (turn) => ({
        ...turn,
        blocks: finishThinkingBlocks(turn.blocks),
        assistantText: turn.assistantText + event.text,
      }));

    case "smith_ui":
      return updateLastTurn(entries, (turn) => ({
        ...turn,
        blocks: [...finishThinkingBlocks(turn.blocks), { id: createId(), type: "smith_ui", payload: event.payload }],
      }));

    case "smith_ui_fallback":
      return updateLastTurn(entries, (turn) => ({
        ...turn,
        blocks: [
          ...finishThinkingBlocks(turn.blocks),
          { id: createId(), type: "smith_ui_fallback", reason: event.reason, code: event.code },
        ],
      }));

    case "thinking":
      return updateLastTurn(entries, (turn) => {
        const blocks = [...turn.blocks];
        const last = blocks[blocks.length - 1];

        if (last?.type === "thinking" && !last.done) {
          const text = event.text || last.text;
          if (event.done && !text.trim()) {
            return {
              ...turn,
              blocks: blocks.slice(0, -1),
            };
          }

          blocks[blocks.length - 1] = {
            ...last,
            text,
            done: event.done,
          };

          return {
            ...turn,
            blocks,
          };
        }

        if (!event.text.trim()) {
          return turn;
        }

        return {
          ...turn,
          blocks: [
            ...blocks,
            {
              id: createId(),
              type: "thinking",
              text: event.text,
              done: event.done,
            },
          ],
        };
      });

    case "tool_call":
      return updateLastTurn(entries, (turn) => applyToolCallToTurn(turn, event));

    case "tool_result":
      return updateLastTurn(entries, (turn) => applyToolResultToTurn(turn, event));

    case "skill":
      return updateLastTurn(entries, (turn) => {
        const blocks = finishThinkingBlocks(turn.blocks);
        const state = nextSkillState(event.status);

        const name = event.name || "skill";

        // Reuse the most recent non-terminal (running/retry) block for this
        // skill instead of appending a duplicate. A domain-gate retry re-emits
        // SKILL_START after a "retry" status; matching retry as well as running
        // lets that repeated start reuse the block, so it can't leave a phantom
        // block stuck forever in the retry state.
        const reversedIndex = [...blocks]
          .reverse()
          .findIndex(
            (block) =>
              block.type === "skill" && block.name === name && (block.state === "running" || block.state === "retry"),
          );

        if (reversedIndex >= 0) {
          const index = blocks.length - 1 - reversedIndex;
          const existing = blocks[index];
          if (existing?.type === "skill") {
            const nextBlocks = [...blocks];
            nextBlocks[index] = { ...existing, state };
            return { ...turn, blocks: nextBlocks };
          }
        }

        return {
          ...turn,
          blocks: [
            ...blocks,
            {
              id: createId(),
              type: "skill",
              name,
              state,
              activities: [],
            },
          ],
        };
      });

    case "route_decided":
      // Pipeline telemetry — the workflow step card already reflects which
      // route is running, no need to also render a system line.
      return entries;

    case "gate_result":
      return entries;

    case "backtrack":
      return entries;

    case "awaiting_input":
      // Update the skill block's state so the workflow card flips to
      // "waiting", but skip the duplicate system notice.
      return markLatestTurnWaitingForInput(entries);

    case "token_usage":
      return entries;

    case "context_usage":
    case "compression":
      return entries;

    case "done":
      // Several legitimate server sequences end a turn without a terminal
      // event for every block: a pipeline node skipped after a backtrack gets
      // no SKILL_END, and an exception between TOOL_CALL_START and its result
      // is smoothed into done(failed).  Closing the turn without converging
      // those blocks leaves a permanent "running" card burned into <Static>.
      if (event.reason === "awaiting_user_input") return markLatestTurnWaitingForInput(entries);
      if (event.status === "failed") return interruptLatestTurn(entries, "error");
      return interruptLatestTurn(entries, "cancelled", "Ended without a result.");
  }
}

/**
 * Rebuild transcript entries from persisted session messages.
 * Consecutive assistant messages (cancel/retry residue) merge into the previous
 * turn; a leading assistant message becomes a turn with empty user text.
 */
export function restoreTranscript(messages: Message[]): TranscriptEntry[] {
  const entries: TranscriptEntry[] = [];

  for (const message of messages) {
    if (message.role === "user") {
      const turn = createTurnEntry(message.content);
      turn.streaming = false;
      entries.push(turn);
      continue;
    }

    const last = entries[entries.length - 1];
    if (last?.kind === "turn") {
      last.assistantText = last.assistantText ? `${last.assistantText}\n\n${message.content}` : message.content;
      continue;
    }

    const orphan = createTurnEntry("");
    orphan.assistantText = message.content;
    orphan.streaming = false;
    entries.push(orphan);
  }

  return entries;
}

/**
 * Split entries into the immutable prefix (rendered once via <Static>) and the
 * still-changing suffix. Only the last turn can be streaming, so everything
 * before the first streaming turn is final.
 */
export function splitTranscript(entries: TranscriptEntry[]): {
  done: TranscriptEntry[];
  active: TranscriptEntry[];
} {
  const firstActive = entries.findIndex((entry) => entry.kind === "turn" && entry.streaming);
  if (firstActive === -1) {
    return { done: entries, active: [] };
  }
  return { done: entries.slice(0, firstActive), active: entries.slice(firstActive) };
}
