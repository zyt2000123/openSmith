import type { SkillState } from "./transcript-state.js";

export type SkillPresentation = {
  heading: string;
  tone: "success" | "warning" | "error";
};

const PRESENTATION: Record<SkillState, SkillPresentation> = {
  running: { heading: "Running Agent...", tone: "warning" },
  retry: { heading: "Retrying Agent...", tone: "warning" },
  waiting: { heading: "Waiting for your input", tone: "warning" },
  done: { heading: "Agent complete", tone: "success" },
  blocked: { heading: "Agent blocked", tone: "warning" },
  error: { heading: "Agent failed", tone: "error" },
  cancelled: { heading: "Agent cancelled", tone: "warning" },
};

export function skillPresentation(state: SkillState): SkillPresentation {
  return PRESENTATION[state];
}
