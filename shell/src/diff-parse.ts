/**
 * Unified-diff parsing and its colour roles, shared by every front end.
 *
 * The terminal wraps these lines to a column budget and paints them with Ink;
 * the desktop lays them out with CSS. Both must agree on what a hunk *is*, on
 * which spans within a line changed, and on which colour means "addition" — so
 * the parsing lives here and only the drawing differs.
 *
 * Pure data and functions: no Ink, no React, no DOM.
 */
import { ASSISTANT, ERROR, MUTED, SUCCESS, WARNING } from "./theme.js";

export type DiffLineKind = "meta" | "file-old" | "file-new" | "hunk" | "deletion" | "addition" | "context";

export type ChangedRange = {
  start: number;
  end: number;
};

export type UnifiedDiffLine = {
  kind: DiffLineKind;
  text: string;
  oldLine: number | null;
  newLine: number | null;
  changedRanges?: ChangedRange[];
};

export type UnifiedDiff = {
  lines: UnifiedDiffLine[];
  numberWidth: number;
};

type Grapheme = {
  text: string;
  start: number;
  end: number;
};

const graphemeSegmenter =
  typeof Intl.Segmenter === "function" ? new Intl.Segmenter(undefined, { granularity: "grapheme" }) : null;

function graphemes(value: string): Grapheme[] {
  if (graphemeSegmenter) {
    return Array.from(graphemeSegmenter.segment(value), ({ segment, index }) => ({
      text: segment,
      start: index,
      end: index + segment.length,
    }));
  }
  let offset = 0;
  return Array.from(value, (text) => {
    const grapheme = { text, start: offset, end: offset + text.length };
    offset += text.length;
    return grapheme;
  });
}

function changedRanges(left: string, right: string): { left: ChangedRange[]; right: ChangedRange[] } {
  const leftParts = graphemes(left);
  const rightParts = graphemes(right);
  let prefix = 0;
  while (
    prefix < leftParts.length &&
    prefix < rightParts.length &&
    leftParts[prefix]?.text === rightParts[prefix]?.text
  ) {
    prefix += 1;
  }

  let suffix = 0;
  while (
    suffix < leftParts.length - prefix &&
    suffix < rightParts.length - prefix &&
    leftParts[leftParts.length - 1 - suffix]?.text === rightParts[rightParts.length - 1 - suffix]?.text
  ) {
    suffix += 1;
  }

  const leftStart = leftParts[prefix]?.start ?? left.length;
  const leftEnd = leftParts[leftParts.length - suffix - 1]?.end ?? leftStart;
  const rightStart = rightParts[prefix]?.start ?? right.length;
  const rightEnd = rightParts[rightParts.length - suffix - 1]?.end ?? rightStart;
  return {
    left: leftStart === leftEnd ? [] : [{ start: leftStart, end: leftEnd }],
    right: rightStart === rightEnd ? [] : [{ start: rightStart, end: rightEnd }],
  };
}

function hunkStart(line: string): { old: number; next: number } | null {
  const match = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/u);
  if (!match?.[1] || !match[2]) return null;
  // A 400-digit line number in a hostile patch parses to Infinity and renders
  // "Infinity" gutters; clamp to what line counters can ever produce.
  const old = Number(match[1]);
  const next = Number(match[2]);
  if (!Number.isFinite(old) || !Number.isFinite(next)) return null;
  return { old, next };
}

type DiffCursor = {
  oldLine: number | null;
  newLine: number | null;
};

function nextOldLine(cursor: DiffCursor): number | null {
  const line = cursor.oldLine;
  cursor.oldLine = line === null ? null : line + 1;
  return line;
}

function nextNewLine(cursor: DiffCursor): number | null {
  const line = cursor.newLine;
  cursor.newLine = line === null ? null : line + 1;
  return line;
}

function parseHunkLine(sourceLine: string, cursor: DiffCursor): UnifiedDiffLine | null {
  const hunk = hunkStart(sourceLine);
  if (!hunk) return null;
  cursor.oldLine = hunk.old;
  cursor.newLine = hunk.next;
  return { kind: "hunk", text: sourceLine, oldLine: null, newLine: null };
}

function parseHeaderLine(sourceLine: string): UnifiedDiffLine | null {
  if (sourceLine.startsWith("diff --git ") || sourceLine.startsWith("index ")) {
    return { kind: "meta", text: sourceLine, oldLine: null, newLine: null };
  }
  if (sourceLine.startsWith("--- ")) return { kind: "file-old", text: sourceLine, oldLine: null, newLine: null };
  if (sourceLine.startsWith("+++ ")) return { kind: "file-new", text: sourceLine, oldLine: null, newLine: null };
  return null;
}

function parseContentLine(sourceLine: string, cursor: DiffCursor): UnifiedDiffLine {
  switch (sourceLine[0]) {
    case "-":
      return { kind: "deletion", text: sourceLine.slice(1), oldLine: nextOldLine(cursor), newLine: null };
    case "+":
      return { kind: "addition", text: sourceLine.slice(1), oldLine: null, newLine: nextNewLine(cursor) };
    case " ":
      return {
        kind: "context",
        text: sourceLine.slice(1),
        oldLine: nextOldLine(cursor),
        newLine: nextNewLine(cursor),
      };
    default:
      return { kind: "meta", text: sourceLine, oldLine: null, newLine: null };
  }
}

// Folds rather than spreading two arguments per numbered line: a machine-sized
// diff would otherwise overflow the call stack instead of rendering.
function numberedLineWidth(lines: UnifiedDiffLine[]): number {
  return lines.reduce((widest, line) => {
    if (line.oldLine === null && line.newLine === null) return widest;
    return Math.max(widest, String(line.oldLine ?? 0).length, String(line.newLine ?? 0).length);
  }, 1);
}

function withWordChanges(lines: UnifiedDiffLine[]): UnifiedDiffLine[] {
  const result = [...lines];
  let index = 0;
  while (index < result.length) {
    const first = result[index];
    if (first?.kind !== "deletion") {
      index += 1;
      continue;
    }

    let deleteEnd = index;
    while (result[deleteEnd]?.kind === "deletion") deleteEnd += 1;
    let addEnd = deleteEnd;
    while (result[addEnd]?.kind === "addition") addEnd += 1;

    const pairs = Math.min(deleteEnd - index, addEnd - deleteEnd);
    for (let offset = 0; offset < pairs; offset += 1) {
      const deletionIndex = index + offset;
      const additionIndex = deleteEnd + offset;
      const deletion = result[deletionIndex];
      const addition = result[additionIndex];
      if (!deletion || !addition) continue;
      const ranges = changedRanges(deletion.text, addition.text);
      result[deletionIndex] = { ...deletion, changedRanges: ranges.left };
      result[additionIndex] = { ...addition, changedRanges: ranges.right };
    }
    index = Math.max(addEnd, index + 1);
  }
  return result;
}

/** Parses a unified diff without depending on the backend's abbreviated tool summaries. */
export function parseUnifiedDiff(source: string): UnifiedDiff {
  const lines: UnifiedDiffLine[] = [];
  const cursor: DiffCursor = { oldLine: null, newLine: null };

  for (const sourceLine of source.replace(/\r\n?/g, "\n").split("\n")) {
    lines.push(
      parseHunkLine(sourceLine, cursor) ?? parseHeaderLine(sourceLine) ?? parseContentLine(sourceLine, cursor),
    );
  }

  return { lines: withWordChanges(lines), numberWidth: numberedLineWidth(lines) };
}

export function diffColor(kind: DiffLineKind): string {
  if (kind === "addition") return SUCCESS;
  if (kind === "deletion") return ERROR;
  if (kind === "hunk") return WARNING;
  if (kind === "file-new") return ASSISTANT;
  return MUTED;
}

export function wordSegments(text: string, ranges: ChangedRange[] | undefined): Array<{ text: string; changed: boolean }> {
  if (!ranges?.length) return [{ text, changed: false }];
  const range = ranges[0];
  if (!range) return [{ text, changed: false }];
  return [
    { text: text.slice(0, range.start), changed: false },
    { text: text.slice(range.start, range.end), changed: true },
    { text: text.slice(range.end), changed: false },
  ].filter((segment) => segment.text.length > 0);
}

export function diffLanguage(source: string): string | undefined {
  const path = source
    .split("\n")
    .find((line) => line.startsWith("+++ "))
    ?.replace(/^\+\+\+ (?:[ab]\/)?/u, "");
  const extension = path?.split(".").pop()?.toLowerCase();
  const languages: Record<string, string> = {
    js: "javascript",
    jsx: "jsx",
    ts: "typescript",
    tsx: "tsx",
    py: "python",
    rs: "rust",
    go: "go",
    json: "json",
    md: "markdown",
  };
  return extension ? languages[extension] : undefined;
}
