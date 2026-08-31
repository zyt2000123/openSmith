/**
 * Rendering decisions shared by every front end.
 *
 * The terminal renders through Ink (ANSI to a TTY) and the desktop through
 * React (DOM); the renderers cannot be the same. What must be the same is what
 * they render — which marker means "running", which colour means "error", what
 * the welcome art is. Those live here because `transcript.tsx` and `index.tsx`
 * import Ink, so nothing outside the terminal can read a constant defined there
 * without dragging a terminal renderer into a browser bundle.
 *
 * Data only: no Ink, no React, no DOM.
 */
import type { ToolState } from "./activity.js";
import { ERROR, MUTED, SUCCESS, WARNING } from "./theme.js";

export const TOOL_PRESENTATION: Record<ToolState, { color: string; marker: string; label: string }> = {
  running: { color: WARNING, marker: "◐", label: "running" },
  success: { color: SUCCESS, marker: "●", label: "success" },
  error: { color: ERROR, marker: "✕", label: "error" },
  blocked: { color: WARNING, marker: "⛔", label: "permission blocked" },
  preflight: { color: WARNING, marker: "◆", label: "fact preflight" },
  cancelled: { color: MUTED, marker: "○", label: "cancelled" },
};

/** Marker for an assistant turn; blinks while the turn is still streaming. */
export const ASSISTANT_MARKER = "●";

/** Prefix drawn before the user's own message. */
export const USER_CARET = "❯";

/** Label above a reasoning block; the terminal appends "..." while it streams. */
export const THINKING_LABEL = "∴ thinking";

export const SMITH_LOGO = [
  "███████╗███╗   ███╗██╗████████╗██╗  ██╗",
  "██╔════╝████╗ ████║██║╚══██╔══╝██║  ██║",
  "███████╗██╔████╔██║██║   ██║   ███████║",
  "╚════██║██║╚██╔╝██║██║   ██║   ██╔══██║",
  "███████║██║ ╚═╝ ██║██║   ██║   ██║  ██║",
  "╚══════╝╚═╝     ╚═╝╚═╝   ╚═╝   ╚═╝  ╚═╝",
];

export const GHOST_BUDDY = ["  ─╥╥─  ", "▄██████▄", "██ ██ ██", " ██████ ", "╰╯╰╮╭╯╰╯"];

export const HERO_HINTS = ["`/` for commands", "`@` for skills", "Enter confirms", "Esc goes back", "`/help` for all"];
