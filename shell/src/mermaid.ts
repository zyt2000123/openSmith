import { renderMermaidASCII } from "beautiful-mermaid";
import stringWidth from "string-width";

import { ASSISTANT, BORDER, INFO } from "./theme.js";

export type MarkdownSegment =
  | { type: "markdown"; text: string }
  | { type: "mermaid"; text: string }
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
const PLACEHOLDER_START = 0xe000;
const MAX_INLINE_EDGE_LABEL_WIDTH = 24;
const EDGE_ANNOTATION_WIDTH = 80;

type NormalizedMermaidDiagram = {
  text: string;
  annotations: string[];
};

type EncodedDiagram = {
  text: string;
  restore: (rendered: string) => string | null;
};

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
  if (fence.language === "mermaid") return { type: "mermaid", text };
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

function normalizeDiagramText(value: string): string {
  return value
    .replace(/<br\s*\/?\s*>/gi, " / ")
    .replace(/\\n/g, " / ")
    .replace(/\s+/g, " ")
    .trim();
}

function graphemes(value: string): string[] {
  if (typeof Intl.Segmenter === "function") {
    const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });
    return Array.from(segmenter.segment(value), ({ segment }) => segment);
  }
  return Array.from(value);
}

/**
 * Mermaid ASCII renderers measure labels with String.length, while terminals measure
 * their visual width in cells. Expand wide graphemes to one-cell placeholders
 * for layout, then restore the original graphemes after drawing.
 */
function encodeTerminalWidths(source: string): EncodedDiagram {
  const encodedByGrapheme = new Map<string, { token: string; value: string }>();
  const replacements = new Map<string, { token: string; value: string }>();
  let nextCodePoint = PLACEHOLDER_START;
  let encoded = "";

  for (const grapheme of graphemes(source)) {
    const width = stringWidth(grapheme);
    if (width <= 1) {
      encoded += grapheme;
      continue;
    }

    let replacement = encodedByGrapheme.get(grapheme);
    if (!replacement) {
      let placeholder = String.fromCodePoint(nextCodePoint++);
      while (source.includes(placeholder) || replacements.has(placeholder)) {
        placeholder = String.fromCodePoint(nextCodePoint++);
      }

      replacement = {
        token: placeholder.repeat(width),
        value: grapheme,
      };
      encodedByGrapheme.set(grapheme, replacement);
      replacements.set(placeholder, replacement);
    }

    encoded += replacement.token;
  }

  return {
    text: encoded,
    restore: (rendered) => {
      let restored = rendered;
      for (const { token, value } of replacements.values()) {
        restored = restored.replaceAll(token, value);
      }
      return [...replacements.keys()].some((placeholder) => restored.includes(placeholder)) ? null : restored;
    },
  };
}

function extractLongEdgeLabels(source: string): { text: string; annotations: string[] } {
  const annotations: string[] = [];
  const text = source.replace(/-->\|((?:\\.|[^|])*)\|/g, (_match: string, rawLabel: string) => {
    const label = normalizeDiagramText(rawLabel);
    if (stringWidth(label) <= MAX_INLINE_EDGE_LABEL_WIDTH) return `-->|${label}|`;

    annotations.push(label);
    return "-->";
  });

  return { text, annotations };
}

function wrapTerminalText(value: string, maxWidth: number): string[] {
  const lines: string[] = [];
  let line = "";
  let width = 0;

  for (const grapheme of graphemes(value)) {
    const graphemeWidth = stringWidth(grapheme);
    if (line && width + graphemeWidth > maxWidth) {
      lines.push(line.trimEnd());
      line = "";
      width = 0;
    }
    if (!line && grapheme === " ") continue;
    line += grapheme;
    width += graphemeWidth;
  }

  if (line) lines.push(line.trimEnd());
  return lines;
}

/**
 * The terminal renderer intentionally supports a small Mermaid grammar. Keep
 * flowchart node IDs and shapes intact so the renderer can preserve the graph
 * structure and lay out CJK labels correctly.
 */
function normalizeMermaidDiagram(source: string): NormalizedMermaidDiagram | null {
  const lines = source.split(/\r?\n/);
  const firstIndex = lines.findIndex((line) => line.trim().length > 0 && !line.trim().startsWith("%%"));
  if (firstIndex < 0) return null;

  const direction = lines[firstIndex]?.trim().match(/^(graph|flowchart)\s+(TD|LR)$/i);
  if (direction) {
    const extracted = extractLongEdgeLabels(source);
    const normalizedLines = extracted.text.split(/\r?\n/);
    return {
      text: normalizedLines
        .map((line, index) =>
          index === firstIndex ? `${direction[1].toLowerCase()} ${direction[2].toUpperCase()}` : line,
        )
        .join("\n"),
      annotations: extracted.annotations,
    };
  }

  if (!/^sequenceDiagram$/i.test(lines[firstIndex]?.trim() ?? "")) return null;

  return {
    text: lines.map((line, index) => (index === firstIndex ? "sequenceDiagram" : line)).join("\n"),
    annotations: [],
  };
}

/** Returns a terminal diagram, or null when the input is not supported. */
export function renderMermaidDiagram(source: string): string | null {
  const normalized = normalizeMermaidDiagram(source);
  if (!normalized) return null;
  const encoded = encodeTerminalWidths(normalized.text);

  const previousDebug = console.debug;
  console.debug = () => undefined;
  try {
    const output = renderMermaidASCII(encoded.text, {
      useAscii: process.env.SMITH_ASCII_DIAGRAMS === "1",
      colorMode: "truecolor",
      // Match the markdown table: grey frame, near-white labels, accent arrows.
      // corner/junction default to a lighter grey than `border`; pin them too so
      // the whole frame reads as one colour.
      theme: { arrow: ASSISTANT, border: BORDER, corner: BORDER, fg: INFO, junction: BORDER, line: BORDER },
    });
    const compact = output
      .trimEnd()
      .split("\n")
      .map((line) => line.trimEnd())
      .join("\n");
    const restored = compact ? encoded.restore(compact) : null;
    if (!restored) return null;

    if (normalized.annotations.length === 0) return restored;

    const annotations = normalized.annotations
      .flatMap((annotation) => wrapTerminalText(`↳ ${annotation}`, EDGE_ANNOTATION_WIDTH))
      .join("\n");
    return annotations ? `${restored}\n\n${annotations}` : restored;
  } catch {
    return null;
  } finally {
    console.debug = previousDebug;
  }
}

type SimpleDiagram = {
  left: string;
  right: string;
  relation: string;
  caption: string;
  direction: "left" | "right";
};

const RELATION_GLYPHS = /[<>←→—–=-]/u;
const RELATION_EDGES = /^[<←—–=-].*[>→—–=-]$/u;
const RELATION_TRIM = /^[\s<>←→—–=-]+|[\s<>←→—–=-]+$/gu;
const CAPTION_PATTERN = /^[（(](.*?)[）)]$/u;

function parseSimpleDiagram(source: string): SimpleDiagram | null {
  const lines = source
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  if (lines.length === 0 || lines.length > 2) return null;

  const columns = lines[0]?.split(/\s{2,}/).filter(Boolean);
  if (columns?.length !== 3) return null;
  const [left, relation, right] = columns;
  if (!left || !relation || !right || !RELATION_GLYPHS.test(relation) || !RELATION_EDGES.test(relation)) return null;

  const hasLeftArrow = /[<←]/u.test(relation);
  const hasRightArrow = /[>→]/u.test(relation);
  if (hasLeftArrow === hasRightArrow) return null;

  const caption = lines[1]?.match(CAPTION_PATTERN)?.[1]?.trim() ?? "";
  if (lines.length === 2 && !caption) return null;

  return {
    left,
    right,
    relation: relation.replace(RELATION_TRIM, "").trim(),
    caption,
    direction: hasLeftArrow ? "left" : "right",
  };
}

function escapeMermaidLabel(value: string): string {
  return value.replaceAll("[", "(").replaceAll("]", ")").replaceAll('"', "'");
}

/** Renders an unlabelled, two-endpoint arrow block when its structure is unambiguous. */
export function renderSimpleTextDiagram(source: string): string | null {
  const diagram = parseSimpleDiagram(source);
  if (!diagram) return null;

  const edgeLabel = [diagram.relation, diagram.caption].filter(Boolean).join(" · ");
  const sourceLabel = diagram.direction === "left" ? diagram.right : diagram.left;
  const targetLabel = diagram.direction === "left" ? diagram.left : diagram.right;
  const edge = edgeLabel ? `-->|${escapeMermaidLabel(edgeLabel)}|` : "-->";
  return renderMermaidDiagram(
    `flowchart LR\n  source[${escapeMermaidLabel(sourceLabel)}] ${edge} target[${escapeMermaidLabel(targetLabel)}]`,
  );
}
