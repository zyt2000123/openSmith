import { Box, Text } from "ink";

import type { CodeHighlighter } from "./code-block.js";
import { displayWidth, wrapDisplayText } from "./text-layout.js";
import { ASSISTANT, BORDER, ERROR, MUTED, SUCCESS, WARNING } from "./theme.js";

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

export type RenderedDiffLine = {
  kind: DiffLineKind;
  prefix: string;
  content: string;
  text: string;
  continuation: boolean;
  changedRanges?: ChangedRange[];
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
  return { old: Number(match[1]), next: Number(match[2]) };
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

function marker(kind: DiffLineKind): string {
  if (kind === "deletion") return "-";
  if (kind === "addition") return "+";
  return " ";
}

function linePrefix(line: UnifiedDiffLine, numberWidth: number): string {
  if (line.oldLine === null && line.newLine === null) return "";
  const oldLine = line.oldLine === null ? " ".repeat(numberWidth) : String(line.oldLine).padStart(numberWidth, " ");
  const newLine = line.newLine === null ? " ".repeat(numberWidth) : String(line.newLine).padStart(numberWidth, " ");
  return `${oldLine} ${newLine} │ ${marker(line.kind)} `;
}

function continuationPrefix(numberWidth: number): string {
  return `${" ".repeat(numberWidth * 2 + 4)}│ `;
}

/** Builds width-safe display lines while retaining gutters on wrapped diff rows. */
export function renderDiffLines(diff: UnifiedDiff, width: number): RenderedDiffLine[] {
  const safeWidth = Math.max(1, Math.floor(width));
  return diff.lines.flatMap((line) => {
    const prefix = linePrefix(line, diff.numberWidth);
    const continuation = continuationPrefix(diff.numberWidth);
    const available = Math.max(1, safeWidth - displayWidth(prefix || "  "));
    const fragments = wrapDisplayText(line.text, {
      width: available,
      breakLongTokens: true,
      preserveWhitespace: true,
    });

    let offset = 0;
    return fragments.map((content, index) => {
      const currentPrefix = index === 0 ? prefix : continuation;
      const fragmentStart = offset;
      offset += content.length;
      // Remap whole-line changedRanges onto this fragment's local offsets and
      // clip to the slice it covers, so an intraline highlight lands on the
      // correct wrapped fragment instead of only the first one.
      const changedRanges = line.changedRanges
        ?.map((range) => ({
          start: Math.max(0, range.start - fragmentStart),
          end: Math.min(content.length, range.end - fragmentStart),
        }))
        .filter((range) => range.start < range.end);
      return {
        kind: line.kind,
        prefix: currentPrefix,
        content,
        text: `${currentPrefix}${content}`,
        continuation: index > 0,
        ...(changedRanges?.length ? { changedRanges } : {}),
      };
    });
  });
}

function diffColor(kind: DiffLineKind): string {
  if (kind === "addition") return SUCCESS;
  if (kind === "deletion") return ERROR;
  if (kind === "hunk") return WARNING;
  if (kind === "file-new") return ASSISTANT;
  return MUTED;
}

function wordSegments(text: string, ranges: ChangedRange[] | undefined): Array<{ text: string; changed: boolean }> {
  if (!ranges?.length) return [{ text, changed: false }];
  const range = ranges[0];
  if (!range) return [{ text, changed: false }];
  return [
    { text: text.slice(0, range.start), changed: false },
    { text: text.slice(range.start, range.end), changed: true },
    { text: text.slice(range.end), changed: false },
  ].filter((segment) => segment.text.length > 0);
}

function diffLanguage(source: string): string | undefined {
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

function highlightedContent(
  content: string,
  language: string | undefined,
  highlighter: CodeHighlighter | undefined,
): string {
  if (!language || !highlighter) return content;
  try {
    const highlighted = highlighter(content, language);
    return highlighted.includes("\n") ? content : highlighted;
  } catch {
    return content;
  }
}

export function DiffBlock({
  source,
  width,
  highlighter,
}: {
  source: string;
  width: number;
  highlighter?: CodeHighlighter;
}) {
  // The parent gives us the full footprint. Borders and horizontal padding
  // consume four cells before a rendered diff line reaches Ink.
  const lines = renderDiffLines(parseUnifiedDiff(source), Math.max(1, width - 4));
  const language = diffLanguage(source);
  const lineCounts = new Map<string, number>();
  return (
    <Box
      flexDirection="column"
      width={Math.max(1, width)}
      marginTop={1}
      marginBottom={1}
      borderColor={BORDER}
      borderStyle="single"
      paddingX={1}
    >
      {lines.map((line) => {
        const basis = `${line.kind}\u0000${line.prefix}\u0000${line.content}`;
        const occurrence = lineCounts.get(basis) ?? 0;
        lineCounts.set(basis, occurrence + 1);
        const segments = wordSegments(line.content, line.changedRanges);
        const segmentCounts = new Map<string, number>();
        return (
          <Text key={`${basis}\u0000${occurrence}`}>
            <Text color={MUTED}>{line.prefix}</Text>
            {segments.map((segment) => {
              const segmentBasis = `${segment.changed}\u0000${segment.text}`;
              const segmentOccurrence = segmentCounts.get(segmentBasis) ?? 0;
              segmentCounts.set(segmentBasis, segmentOccurrence + 1);
              const content = segment.changed ? segment.text : highlightedContent(segment.text, language, highlighter);
              return (
                <Text
                  key={`${segmentBasis}\u0000${segmentOccurrence}`}
                  color={diffColor(line.kind)}
                  bold={segment.changed}
                >
                  {content || " "}
                </Text>
              );
            })}
          </Text>
        );
      })}
    </Box>
  );
}
