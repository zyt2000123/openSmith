import { MarkdownText } from "@assistant-ui/react-ink-markdown";
import { Box, Text } from "ink";
import { marked, type Token, type Tokens } from "marked";
import { Fragment } from "react";

import { displayWidth, padDisplayText, type TextAlignment, wrapDisplayText } from "./text-layout.js";
import { BORDER, INFO } from "./theme.js";

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

/** Padded cell text for one visual line of a row; the column rules are excluded. */
function rowCellLines(layout: MarkdownTableLayout, row: GridTableRow): string[][] {
  const height = Math.max(1, ...row.cells.map((cell) => cell.lines.length));
  return Array.from({ length: height }, (_, lineIndex) =>
    row.cells.map((cell, columnIndex) => {
      const content = cell.lines[lineIndex] ?? "";
      const width = layout.columnWidths[columnIndex] ?? 1;
      return `${" ".repeat(layout.padding)}${padDisplayText(content, width, cell.alignment)}${" ".repeat(layout.padding)}`;
    }),
  );
}

export type MarkdownTableGridLine =
  | { kind: "border"; text: string }
  | { kind: "cells"; header: boolean; cells: string[] };

/**
 * The grid with cell text kept apart from the column rules. Colour-aware
 * rendering must consume this instead of re-splitting the joined line: a cell
 * may legitimately contain the rule character itself, and splitting on it would
 * paint that character as a border and cut the cell in two.
 */
export function buildMarkdownTableGrid(layout: MarkdownTableLayout): MarkdownTableGridLine[] {
  if (layout.rows.length === 0) return [];
  const grid: MarkdownTableGridLine[] = [{ kind: "border", text: borderLine(layout, "┌", "┬", "┐") }];
  for (const [index, row] of layout.rows.entries()) {
    for (const cells of rowCellLines(layout, row)) {
      grid.push({ kind: "cells", header: row.header, cells });
    }
    // A header-only table still gets its separator, matching the original grid.
    if (index === 0 || index < layout.rows.length - 1) {
      grid.push({ kind: "border", text: borderLine(layout, "├", "┼", "┤") });
    }
  }
  grid.push({ kind: "border", text: borderLine(layout, "└", "┴", "┘") });
  return grid;
}

/** Returns plain grid lines so terminal-width behavior is testable without Ink. */
export function renderMarkdownTableLines(layout: MarkdownTableLayout): string[] {
  return buildMarkdownTableGrid(layout).map((line) =>
    line.kind === "border" ? line.text : `│${line.cells.join("│")}│`,
  );
}

export function MarkdownTableBlock({ markdown, width }: { markdown: string; width: number }) {
  const table = parseMarkdownTable(markdown);
  if (!table) {
    // Same hardening as the transcript renderer: never emit OSC-8 hyperlinks
    // (file:///javascript:/data: targets are model-authored).
    return <MarkdownText text={markdown} width={width} hyperlinks={false} />;
  }

  // Rules are emitted as their own <Text> so they keep the border colour while
  // cell text keeps the content colour; cell strings are never re-parsed.
  return (
    <Box flexDirection="column">
      {buildMarkdownTableGrid(layoutMarkdownTable(table, width)).map((line, lineIndex) => {
        const key = `line-${lineIndex}`;
        if (line.kind === "border") {
          return (
            <Text color={BORDER} key={key}>
              {line.text}
            </Text>
          );
        }
        return (
          <Text key={key}>
            <Text color={BORDER}>│</Text>
            {line.cells.map((cell, column) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: column position is fixed for the whole grid.
              <Fragment key={`${key}-${column}`}>
                <Text bold={line.header} color={INFO}>
                  {cell}
                </Text>
                <Text color={BORDER}>│</Text>
              </Fragment>
            ))}
          </Text>
        );
      })}
    </Box>
  );
}
