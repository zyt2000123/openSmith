export type StreamingMarkdownSnapshot = {
  /** Complete Markdown that is safe to treat as a stable prefix. */
  stable: string;
  /** The incomplete line or multi-line construct that must remain mutable. */
  pending: string;
};

type Fence = {
  marker: "`" | "~";
  length: number;
};

type ScannerState = {
  stable: string[];
  held: string[];
  pendingHeader: string | null;
  tableOpen: boolean;
  fence: Fence | null;
};

function fenceStart(line: string): Fence | null {
  const match = line.match(/^( {0,3})(`{3,}|~{3,})(.*)$/);
  if (!match?.[2]) return null;
  if (match[2][0] === "`" && match[3]?.includes("`")) return null;
  return { marker: match[2][0] as "`" | "~", length: match[2].length };
}

function fenceEnd(line: string, fence: Fence): boolean {
  const match = line.match(/^( {0,3})(`{3,}|~{3,})\s*$/);
  return Boolean(match?.[2] && match[2][0] === fence.marker && match[2].length >= fence.length);
}

function looksLikeTableHeader(line: string): boolean {
  return line.includes("|") && /[^\s|]/u.test(line);
}

function isTableSeparator(line: string): boolean {
  return /^\|?(?:\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?$/u.test(line.trim());
}

function isTableRow(line: string): boolean {
  return line.includes("|") && /[^\s|]/u.test(line);
}

function flushHeld(state: ScannerState): void {
  state.stable.push(...state.held);
  state.held = [];
}

function consumeFenceLine(state: ScannerState, line: string, serialized: string): boolean {
  if (!state.fence) return false;
  state.held.push(serialized);
  if (fenceEnd(line, state.fence)) {
    state.fence = null;
    flushHeld(state);
  }
  return true;
}

function consumeTableLine(state: ScannerState, line: string, serialized: string): boolean {
  if (!state.tableOpen) return false;
  if (line.trim() && isTableRow(line)) {
    state.held.push(serialized);
    return true;
  }
  state.tableOpen = false;
  flushHeld(state);
  return false;
}

function consumePendingHeader(state: ScannerState, line: string, serialized: string): boolean {
  if (state.pendingHeader === null) return false;
  if (isTableSeparator(line)) {
    state.tableOpen = true;
    state.held = [`${state.pendingHeader}\n`, serialized];
    state.pendingHeader = null;
    return true;
  }
  state.stable.push(`${state.pendingHeader}\n`);
  state.pendingHeader = null;
  return false;
}

function startHeldConstruct(state: ScannerState, line: string, serialized: string): boolean {
  const openingFence = fenceStart(line);
  if (openingFence) {
    state.fence = openingFence;
    state.held = [serialized];
    return true;
  }
  if (looksLikeTableHeader(line)) {
    state.pendingHeader = line;
    return true;
  }
  return false;
}

function processCompleteLine(state: ScannerState, line: string): void {
  const serialized = `${line}\n`;
  if (consumeFenceLine(state, line, serialized)) return;
  if (consumeTableLine(state, line, serialized)) return;
  if (consumePendingHeader(state, line, serialized)) return;
  if (startHeldConstruct(state, line, serialized)) return;
  state.stable.push(serialized);
}

/**
 * Separates complete Markdown from an unfinished table/fence tail. The Shell
 * keeps the tail in the dynamic React region; the function deliberately has
 * no Bridge or Store state so it cannot change stream lifecycle semantics.
 */
export function splitStreamingMarkdown(markdown: string, streaming: boolean): StreamingMarkdownSnapshot {
  const normalized = markdown.replace(/\r\n?/g, "\n");
  if (!streaming) return { stable: normalized, pending: "" };

  const chunks = normalized.split("\n");
  const hasPartialLine = !normalized.endsWith("\n");
  const partialLine = hasPartialLine ? (chunks.pop() ?? "") : "";
  if (!hasPartialLine) chunks.pop();

  const state: ScannerState = { stable: [], held: [], pendingHeader: null, tableOpen: false, fence: null };
  for (const line of chunks) processCompleteLine(state, line);

  const pending = [
    ...state.held,
    ...(state.pendingHeader === null ? [] : [`${state.pendingHeader}\n`]),
    partialLine,
  ].join("");
  return { stable: state.stable.join(""), pending };
}
