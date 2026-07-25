/** Splits assistant markdown into fenced segments for the transcript renderer. */

export type MarkdownSegment =
  | { type: "markdown"; text: string }
  | { type: "diff"; language: "diff" | "patch"; text: string }
  | { type: "code"; language: string; text: string };

type OpenFence = {
  marker: "`" | "~";
  length: number;
  line: string;
  language: string;
  body: string[];
};

const FENCE_PATTERN = /^( {0,3})(`{3,}|~{3,})\s*([^\s`~]*)/;
function pushMarkdown(segments: MarkdownSegment[], lines: string[]): void {
  const text = lines.join("\n");
  if (text.length > 0) segments.push({ type: "markdown", text });
}

function isClosingFence(line: string, fence: OpenFence): boolean {
  const match = line.match(/^( {0,3})(`{3,}|~{3,})\s*$/);
  return Boolean(match && match[2]?.[0] === fence.marker && match[2].length >= fence.length);
}

function fencedSegment(fence: OpenFence): MarkdownSegment {
  const text = fence.body.join("\n");
  if (fence.language === "diff" || fence.language === "patch") {
    return { type: "diff", language: fence.language, text };
  }
  return { type: "code", language: fence.language, text };
}

function openFence(line: string, opening: RegExpMatchArray): OpenFence {
  return {
    marker: opening[2][0] as "`" | "~",
    length: opening[2].length,
    line,
    language: opening[3]?.toLowerCase() || "text",
    body: [],
  };
}

/** Splits fenced code blocks from surrounding Markdown. */
export function splitMarkdownBlocks(markdown: string): MarkdownSegment[] {
  const segments: MarkdownSegment[] = [];
  const pendingMarkdown: string[] = [];
  let fence: OpenFence | null = null;

  for (const line of markdown.split("\n")) {
    if (!fence) {
      const opening = line.match(FENCE_PATTERN);
      if (opening) {
        pushMarkdown(segments, pendingMarkdown.splice(0));
        fence = openFence(line, opening);
        continue;
      }

      pendingMarkdown.push(line);
      continue;
    }

    if (isClosingFence(line, fence)) {
      segments.push(fencedSegment(fence));
      fence = null;
      continue;
    }

    fence.body.push(line);
  }

  if (fence) {
    pendingMarkdown.push(fence.line, ...fence.body);
  }
  pushMarkdown(segments, pendingMarkdown);
  return segments;
}
