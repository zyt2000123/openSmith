import { Box, Text } from "ink";
import { useMemo } from "react";

import { BORDER, MUTED } from "./theme.js";

export type CodeHighlighter = (code: string, language?: string) => string;

export type CodeLine = {
  number: string;
  text: string;
};

export function formatCodeLines(code: string, highlighter?: CodeHighlighter, language?: string): CodeLine[] {
  let highlighted = code;
  try {
    if (highlighter) highlighted = highlighter(code, language);
  } catch {
    highlighted = code;
  }
  const lines = highlighted.split("\n");
  const width = String(lines.length).length;

  return lines.map((text, index) => ({
    number: String(index + 1).padStart(width, " "),
    text,
  }));
}

export function CodeBlock({
  code,
  language,
  highlighter,
}: {
  code: string;
  language?: string;
  highlighter?: CodeHighlighter;
}) {
  const lines = useMemo(() => formatCodeLines(code, highlighter, language), [code, highlighter, language]);
  const displayLanguage = language || "text";

  // No explicit width: a `columns - n` budget has to re-derive every ancestor's
  // padding by hand, and when that sum is wrong Ink does not shrink the box — it
  // folds the right border onto the next row. The frame fills its parent through
  // the default `alignItems: stretch` instead. Do not add flexGrow here: the
  // parent is a column, so it would grow the box vertically, not horizontally.
  return (
    <Box flexDirection="column" borderStyle="single" borderColor={BORDER} paddingX={1} marginBottom={1}>
      <Text color={MUTED} dimColor>
        [{displayLanguage}] · {lines.length} 行
      </Text>
      {lines.map((line) => (
        <Text key={`${line.number}-${line.text}`}>
          <Text color={MUTED} dimColor>
            {line.number} │{" "}
          </Text>
          {line.text}
        </Text>
      ))}
    </Box>
  );
}
