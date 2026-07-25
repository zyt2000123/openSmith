import { MarkdownText } from "@assistant-ui/react-ink-markdown";
import { Box, Text } from "ink";
import { marked, type Token, type Tokens } from "marked";

import { displayWidth, padDisplayText, type TextAlignment, wrapDisplayText } from "./text-layout.js";
import { ASSISTANT, BORDER, WARNING } from "./theme.js";

export type MarkdownTable = {
  headers: string[];
  alignments: TextAlignment[];
  rows: string[][];
};

export type GridTableCell = {
  lines: string[];
  alignment: TextAlignment;
};

export type GridTableRow = {
  header: boolean;
  cells: GridTableCell[];
};

export type MarkdownTableLayout = {
  columnWidths: number[];
  padding: number;
  width: number;
  overflowed: boolean;
  rows: GridTableRow[];
};

export type GridTableLine = {
  kind: "border" | "header" | "body";
  text: string;
};

function plainInlineText(tokens: Token[]): string {
  return tokens
    .map((token) => {
      switch (token.type) {
        case "br":
          return "\n";
        case "codespan":
        case "escape":
        case "text":
          return token.text;
        case "strong":
        case "em":
        case "del":
        case "link":
        case "image":
          return plainInlineText(token.tokens ?? []);
        default:
          return "text" in token && typeof token.text === "string" ? token.text : "";
      }
    })
    .join("");
}

function normalizedAlignment(value: Tokens.Table["align"][number]): TextAlignment {
  return value ?? "left";
}

/** Parses the GFM table AST; this deliberately does not split cells on `|`. */
export function parseMarkdownTable(markdown: string): MarkdownTable | null {
  let table: Tokens.Table | undefined;
  try {
    table = marked.lexer(markdown, { gfm: true }).find((token): token is Tokens.Table => token.type === "table");
  } catch {
    return null;
  }
  if (!table || table.header.length === 0) return null;

  const columnCount = table.header.length;
  const textForCell = (cell: Tokens.TableCell | undefined): string => {
    if (!cell) return "";
    return plainInlineText(cell.tokens);
  };

  return {
    headers: table.header.map(textForCell),
    alignments: Array.from({ length: columnCount }, (_, index) => normalizedAlignment(table.align[index] ?? null)),
    rows: table.rows.map((row) => Array.from({ length: columnCount }, (_, index) => textForCell(row[index]))),
  };
}

function graphemes(value: string): string[] {
  if (typeof Intl.Segmenter === "function") {
    const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });
    return Array.from(segmenter.segment(value), ({ segment }) => segment);
  }
  return Array.from(value);
}

function requiredGridWidth(columnWidths: readonly number[], padding: number): number {
  return (
    columnWidths.length + 1 + columnWidths.reduce((sum, width) => sum + width, 0) + columnWidths.length * padding * 2
  );
}

/** A cell cannot be narrower than one complete terminal grapheme. */
// Both fold instead of spreading: one argument per grapheme (or per line) overflows
// the call stack past ~100k arguments, and table content is model-generated.
function minimumColumnWidths(table: MarkdownTable): number[] {
  return table.headers.map((header, index) => {
    const values = [header, ...table.rows.map((row) => row[index] ?? "")];
    return values.reduce(
      (widest, value) => graphemes(value).reduce((cell, grapheme) => Math.max(cell, displayWidth(grapheme)), widest),
      1,
    );
  });
}

function desiredColumnWidths(table: MarkdownTable): number[] {
  return table.headers.map((header, index) => {
    const values = [header, ...table.rows.map((row) => row[index] ?? "")];
    return values.reduce(
      (widest, value) => value.split("\n").reduce((cell, line) => Math.max(cell, displayWidth(line)), widest),
      1,
    );
  });
}

function allocateColumnWidths(desired: number[], minimums: number[], capacity: number): number[] {
  const widths = [...minimums];
  const minimumTotal = widths.reduce((sum, width) => sum + width, 0);
  const target = Math.max(
    minimumTotal,
    Math.min(
      capacity,
      desired.reduce((sum, width) => sum + width, 0),
    ),
  );

  // Give each column a useful initial share before favouring the longest cell.
  // Otherwise a single checksum/path can consume every spare cell and turn
  // short labels such as "execution" into one character per terminal row.
  const usefulMinimum = Math.max(1, Math.min(12, Math.floor(target / widths.length)));
  for (let index = 0; index < widths.length; index += 1) {
    const next = Math.max(minimums[index] ?? 1, Math.min(desired[index] ?? 1, usefulMinimum));
    widths[index] = Math.min(
      next,
      target - widths.reduce((sum, width, offset) => sum + (offset === index ? 0 : width), 0),
    );
  }

  while (widths.reduce((sum, width) => sum + width, 0) < target) {
    let candidate = 0;
    for (let index = 1; index < widths.length; index += 1) {
      const candidateDeficit = (desired[candidate] ?? 0) - (widths[candidate] ?? 0);
      const currentDeficit = (desired[index] ?? 0) - (widths[index] ?? 0);
      if (currentDeficit > candidateDeficit) candidate = index;
    }
    widths[candidate] = (widths[candidate] ?? 0) + 1;
  }

  return widths;
}

/**
 * Computes a content-preserving grid. If a viewport cannot even fit one
 * terminal cell per column and its border, `overflowed` records that physical
 * constraint rather than hiding or transforming the data.
 */
export function layoutMarkdownTable(table: MarkdownTable, requestedWidth: number): MarkdownTableLayout {
  const columnCount = table.headers.length;
  if (columnCount === 0) {
    return { columnWidths: [], padding: 0, width: 0, overflowed: false, rows: [] };
  }

  const safeWidth = Number.isFinite(requestedWidth) ? Math.max(1, Math.floor(requestedWidth)) : 1;
  const minimums = minimumColumnWidths(table);
  const preferredPadding = safeWidth >= requiredGridWidth(minimums, 1) ? 1 : 0;
  const minimumWidth = requiredGridWidth(minimums, preferredPadding);
  const overflowed = safeWidth < minimumWidth;
  const width = Math.max(safeWidth, minimumWidth);
  const contentCapacity = width - (columnCount + 1) - columnCount * preferredPadding * 2;
  const columnWidths = allocateColumnWidths(desiredColumnWidths(table), minimums, contentCapacity);
  const buildRow = (values: string[], header: boolean): GridTableRow => ({
    header,
    cells: values.map((value, index) => ({
      lines: wrapDisplayText(value, { width: columnWidths[index] ?? 1, breakLongTokens: true }),
      alignment: table.alignments[index] ?? "left",
    })),
  });

  return {
    columnWidths,
    padding: preferredPadding,
    width,
    overflowed,
    rows: [buildRow(table.headers, true), ...table.rows.map((row) => buildRow(row, false))],
  };
}

function borderLine(layout: MarkdownTableLayout, left: string, join: string, right: string): string {
  const spans = layout.columnWidths.map((width) => "─".repeat(width + layout.padding * 2));
  return `${left}${spans.join(join)}${right}`;
}

function rowLines(layout: MarkdownTableLayout, row: GridTableRow): string[] {
  const height = Math.max(1, ...row.cells.map((cell) => cell.lines.length));
  return Array.from({ length: height }, (_, lineIndex) => {
    const cells = row.cells.map((cell, columnIndex) => {
      const content = cell.lines[lineIndex] ?? "";
      const width = layout.columnWidths[columnIndex] ?? 1;
      return `${" ".repeat(layout.padding)}${padDisplayText(content, width, cell.alignment)}${" ".repeat(layout.padding)}`;
    });
    return `│${cells.join("│")}│`;
  });
}

/** Returns plain grid lines so terminal-width behavior is testable without Ink. */
export function renderMarkdownTableLines(layout: MarkdownTableLayout): string[] {
  if (layout.rows.length === 0) return [];
  const lines: string[] = [borderLine(layout, "┌", "┬", "┐")];
  for (const [index, row] of layout.rows.entries()) {
    lines.push(...rowLines(layout, row));
    if (index === 0) lines.push(borderLine(layout, "├", "┼", "┤"));
    else if (index < layout.rows.length - 1) lines.push(borderLine(layout, "├", "┼", "┤"));
  }
  lines.push(borderLine(layout, "└", "┴", "┘"));
  return lines;
}

export function MarkdownTableBlock({ markdown, width }: { markdown: string; width: number }) {
  const table = parseMarkdownTable(markdown);
  if (!table) {
    return <MarkdownText text={markdown} width={width} />;
  }

  const layout = layoutMarkdownTable(table, width);
  const lines = renderMarkdownTableLines(layout);
  const lineCounts = new Map<string, number>();
  let sawHeader = false;
  let headerOpen = true;
  return (
    <Box flexDirection="column">
      {lines.map((line) => {
        const isBorder = /^[┌├└]/u.test(line);
        if (isBorder && sawHeader) headerOpen = false;
        const isHeader = !isBorder && headerOpen;
        if (isHeader) sawHeader = true;
        const occurrence = lineCounts.get(line) ?? 0;
        lineCounts.set(line, occurrence + 1);
        return (
          <Text key={`${line}\u0000${occurrence}`} color={isBorder ? BORDER : isHeader ? WARNING : ASSISTANT}>
            {line}
          </Text>
        );
      })}
    </Box>
  );
}
