export type MarkdownLayoutBlock = {
  kind: "content" | "table";
  text: string;
};

type FenceState = {
  marker: "`" | "~";
  length: number;
};

function openingFence(line: string): FenceState | null {
  const opening = line.match(/^( {0,3})(`{3,}|~{3,})(.*)$/);
  if (!opening?.[2]) return null;
  if (opening[2][0] === "`" && opening[3]?.includes("`")) return null;
  return {
    marker: opening[2][0] as "`" | "~",
    length: opening[2].length,
  };
}

function pipeCells(line: string): string[] | null {
  const trimmed = line.trim();
  if (!trimmed.includes("|")) return null;

  const withoutEdges = trimmed.replace(/^\|/, "").replace(/\|$/, "");
  const cells = withoutEdges.split("|");
  return cells.length >= 2 ? cells : null;
}

function isTableSeparator(line: string): boolean {
  const cells = pipeCells(line);
  return Boolean(cells?.every((cell) => /^\s*:?-{1,}:?\s*$/.test(cell)));
}

function isTableRow(line: string): boolean {
  return pipeCells(line) !== null;
}

function isTableStart(lines: string[], index: number): boolean {
  const header = lines[index];
  const separator = lines[index + 1];
  return Boolean(header && separator && isTableRow(header) && isTableSeparator(separator));
}

function trimBlockText(lines: string[]): string {
  return lines.join("\n").replace(/^\n+|\n+$/g, "");
}

function fenceTransition(line: string, current: FenceState | null): FenceState | null | undefined {
  if (current) {
    const closing = line.match(/^( {0,3})(`{3,}|~{3,})\s*$/);
    if (closing && closing[2]?.[0] === current.marker && closing[2].length >= current.length) {
      return null;
    }
    return current;
  }

  return openingFence(line) ?? undefined;
}

function tableEnd(lines: string[], start: number): number {
  let end = start + 2;
  while (end < lines.length && lines[end]?.trim() && isTableRow(lines[end] ?? "")) {
    end++;
  }
  return end;
}

/**
 * Splits only table boundaries that need independent terminal layout. Headings
 * stay with surrounding Markdown so markdansi controls their natural spacing.
 */
export function splitMarkdownLayoutBlocks(markdown: string): MarkdownLayoutBlock[] {
  const lines = markdown.replace(/\r\n/g, "\n").split("\n");
  const blocks: MarkdownLayoutBlock[] = [];
  let blockStart = 0;
  let fence: FenceState | null = null;

  const flushContent = (end: number): void => {
    const text = trimBlockText(lines.slice(blockStart, end));
    if (text) blocks.push({ kind: "content", text });
  };

  let index = 0;
  while (index < lines.length) {
    const line = lines[index] ?? "";

    const nextFence = fenceTransition(line, fence);
    if (nextFence !== undefined) {
      fence = nextFence;
      index++;
      continue;
    }

    if (isTableStart(lines, index)) {
      flushContent(index);
      const end = tableEnd(lines, index);
      const text = trimBlockText(lines.slice(index, end));
      if (text) blocks.push({ kind: "table", text });
      index = end;
      blockStart = index;
      continue;
    }

    index++;
  }

  flushContent(lines.length);
  return blocks;
}
