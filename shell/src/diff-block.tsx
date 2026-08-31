import { Box, Text } from "ink";

import type { CodeHighlighter } from "./code-block.js";
import {
  type ChangedRange,
  type DiffLineKind,
  diffColor,
  diffLanguage,
  parseUnifiedDiff,
  type UnifiedDiff,
  type UnifiedDiffLine,
  wordSegments,
} from "./diff-parse.js";
import { displayWidth, wrapDisplayText } from "./text-layout.js";
import { BORDER, MUTED } from "./theme.js";


export type RenderedDiffLine = {
  kind: DiffLineKind;
  prefix: string;
  content: string;
  text: string;
  continuation: boolean;
  changedRanges?: ChangedRange[];
};


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
