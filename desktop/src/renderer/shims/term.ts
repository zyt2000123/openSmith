/**
 * Replaces shell/src/term.ts in the renderer.
 *
 * The terminal build wipes the screen because Ink's <Static> is append-only.
 * The DOM transcript is re-rendered from state, so there is nothing to wipe —
 * the store already dropped the entries by the time this is called.
 */
export function clearTerminal(): void {
  // ponytail: intentionally empty; DOM re-renders from state.
}
