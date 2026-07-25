import type { StreamEvent } from "./api.js";

export type ToolState = "running" | "success" | "error" | "blocked" | "preflight";

export const TOOL_ACTIVITY_CALL_LIMIT = 256;

type ToolCall = {
  name: string;
  state: ToolState;
};

type ToolCounter = Record<string, number>;

export type ToolActivity = {
  calls: Record<string, ToolCall>;
  running: Record<string, string>;
  successes: ToolCounter;
  errors: ToolCounter;
  blocked: ToolCounter;
  preflight: ToolCounter;
};

export function createToolActivity(): ToolActivity {
  return {
    calls: {},
    running: {},
    successes: {},
    errors: {},
    blocked: {},
    preflight: {},
  };
}

export function toolStateFromResult(event: Extract<StreamEvent, { type: "tool_result" }>): ToolState {
  if (event.preflight) return "preflight";
  if (event.blocked) return "blocked";
  if (event.error) return "error";
  return "success";
}

function changeCount(counter: ToolCounter, name: string, amount: number): ToolCounter {
  const next = (counter[name] ?? 0) + amount;
  if (next > 0) {
    return { ...counter, [name]: next };
  }

  const { [name]: _removed, ...remaining } = counter;
  return remaining;
}

function changeStateCount(activity: ToolActivity, state: ToolState, name: string, amount: number): ToolActivity {
  switch (state) {
    case "success":
      return { ...activity, successes: changeCount(activity.successes, name, amount) };
    case "error":
      return { ...activity, errors: changeCount(activity.errors, name, amount) };
    case "blocked":
      return { ...activity, blocked: changeCount(activity.blocked, name, amount) };
    case "preflight":
      return { ...activity, preflight: changeCount(activity.preflight, name, amount) };
    case "running":
      return activity;
  }
}

/**
 * Bounded at TOOL_ACTIVITY_CALL_LIMIT by dropping settled calls; running ones stay so
 * they can still be settled. Ceiling: past that many calls the dedupe key for a settled
 * id is gone, so a re-sent tool_result for it would count twice. Nothing re-sends one
 * today — a resumed run resets this map wholesale. Add an id->state ledger if that changes.
 */
function pruneCalls(calls: Record<string, ToolCall>): Record<string, ToolCall> {
  if (Object.keys(calls).length <= TOOL_ACTIVITY_CALL_LIMIT) return calls;

  const next = { ...calls };
  for (const [id, call] of Object.entries(calls)) {
    if (Object.keys(next).length <= TOOL_ACTIVITY_CALL_LIMIT) break;
    if (call.state !== "running") delete next[id];
  }
  return next;
}

function startTool(activity: ToolActivity, event: Extract<StreamEvent, { type: "tool_call" }>): ToolActivity {
  if (!event.id) return activity;

  const previous = activity.calls[event.id];
  if (previous?.state === "running" && previous.name === event.name) return activity;
  if (previous && previous.state !== "running") return activity;

  const name = event.name || previous?.name || "tool";
  return {
    ...activity,
    calls: pruneCalls({ ...activity.calls, [event.id]: { name, state: "running" } }),
    running: { ...activity.running, [event.id]: name },
  };
}

function settleTool(activity: ToolActivity, event: Extract<StreamEvent, { type: "tool_result" }>): ToolActivity {
  if (!event.id) return activity;

  const previous = activity.calls[event.id];
  const name = previous?.name || "tool";
  const nextState = toolStateFromResult(event);
  if (previous?.state === nextState) return activity;

  let next: ToolActivity = {
    ...activity,
    calls: pruneCalls({ ...activity.calls, [event.id]: { name, state: nextState } }),
  };
  if (previous?.state === "running") {
    const { [event.id]: _removed, ...running } = next.running;
    next = { ...next, running };
  } else if (previous) {
    next = changeStateCount(next, previous.state, name, -1);
  }

  return changeStateCount(next, nextState, name, 1);
}

export function applyToolActivity(activity: ToolActivity, event: StreamEvent): ToolActivity {
  if (event.type === "tool_call") return startTool(activity, event);
  if (event.type === "tool_result") return settleTool(activity, event);
  return activity;
}
