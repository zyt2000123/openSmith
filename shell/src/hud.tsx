import { execFile } from "node:child_process";
import { Box, Text } from "ink";
import { Fragment, memo, type ReactNode, useEffect, useState } from "react";

import type { ToolActivity } from "./activity.js";
import { type ContextUsage, fetchMemoryMaintenance, type MemoryMaintenance, type TokenUsage } from "./api.js";
import { BORDER, ERROR, GIT, MODEL, MUTED, PROJECT, SESSION, SUCCESS, WARNING } from "./theme.js";
import type { TranscriptViewMode } from "./transcript-state.js";
import { useWindowSize } from "./window-size.js";

type HudSegment = {
  text: string;
  color?: string;
  bold?: boolean;
};

type HudPart = HudSegment[];

const SEP_WIDTH = 3;
const GRAPHEME_SEGMENTER =
  typeof Intl.Segmenter === "function" ? new Intl.Segmenter(undefined, { granularity: "grapheme" }) : null;

function segmentGraphemes(text: string): string[] {
  if (!text) return [];
  if (!GRAPHEME_SEGMENTER) return Array.from(text);
  return Array.from(GRAPHEME_SEGMENTER.segment(text), (segment) => segment.segment);
}

function isFullWidthCodePoint(codePoint: number): boolean {
  return (
    codePoint >= 0x1100 &&
    (codePoint <= 0x115f ||
      codePoint === 0x2329 ||
      codePoint === 0x232a ||
      (codePoint >= 0x2e80 && codePoint <= 0xa4cf && codePoint !== 0x303f) ||
      (codePoint >= 0xac00 && codePoint <= 0xd7a3) ||
      (codePoint >= 0xf900 && codePoint <= 0xfaff) ||
      (codePoint >= 0xfe10 && codePoint <= 0xfe19) ||
      (codePoint >= 0xfe30 && codePoint <= 0xfe6f) ||
      (codePoint >= 0xff00 && codePoint <= 0xff60) ||
      (codePoint >= 0xffe0 && codePoint <= 0xffe6) ||
      (codePoint >= 0x1f300 && codePoint <= 0x1faff) ||
      (codePoint >= 0x20000 && codePoint <= 0x3fffd))
  );
}

function graphemeWidth(grapheme: string): number {
  if (!grapheme || /^\p{Control}$/u.test(grapheme)) return 0;
  if (/\p{Extended_Pictographic}/u.test(grapheme)) return 2;

  let width = 0;
  let hasVisibleBase = false;
  for (const char of Array.from(grapheme)) {
    if (/^\p{Mark}$/u.test(char) || char === "\u200D" || char === "\uFE0F") continue;
    hasVisibleBase = true;
    const codePoint = char.codePointAt(0);
    width = Math.max(width, codePoint && isFullWidthCodePoint(codePoint) ? 2 : 1);
  }

  return hasVisibleBase ? width : 0;
}

function textWidth(text: string): number {
  return segmentGraphemes(text).reduce((total, grapheme) => total + graphemeWidth(grapheme), 0);
}

function partWidth(part: HudPart): number {
  return part.reduce((total, segment) => total + textWidth(segment.text), 0);
}

function lineWidth(parts: HudPart[]): number {
  return parts.reduce((total, part, index) => total + partWidth(part) + (index > 0 ? SEP_WIDTH : 0), 0);
}

function takeTextByWidth(text: string, maxWidth: number): string {
  let used = 0;
  let result = "";
  for (const grapheme of segmentGraphemes(text)) {
    const next = graphemeWidth(grapheme);
    if (used + next > maxWidth) break;
    result += grapheme;
    used += next;
  }
  return result;
}

function truncatePart(part: HudPart, maxWidth: number): HudPart {
  if (maxWidth <= 0 || partWidth(part) <= maxWidth) return part;

  const suffix = maxWidth >= 2 ? "…" : "";
  const limit = Math.max(0, maxWidth - textWidth(suffix));
  let used = 0;
  const result: HudPart = [];

  for (const segment of part) {
    const available = limit - used;
    if (available <= 0) break;

    const kept = takeTextByWidth(segment.text, available);
    if (kept) {
      result.push({ ...segment, text: kept });
      used += textWidth(kept);
    }
    if (kept.length < segment.text.length) break;
  }

  if (suffix) {
    const last = result.at(-1);
    result.push({ text: suffix, color: last?.color });
  }
  return result.length > 0 ? result : [{ text: suffix || ".", color: MUTED }];
}

function wrapParts(parts: HudPart[], maxWidth: number): HudPart[][] {
  if (maxWidth <= 0) return [parts];

  const lines: HudPart[][] = [];
  let current: HudPart[] = [];

  for (const part of parts) {
    const next = current.length === 0 ? [part] : [...current, part];
    if (current.length > 0 && lineWidth(next) > maxWidth) {
      lines.push(current);
      current = [partWidth(part) > maxWidth ? truncatePart(part, maxWidth) : part];
      continue;
    }

    current = next.map((p) => (partWidth(p) > maxWidth ? truncatePart(p, maxWidth) : p));
  }

  if (current.length > 0) lines.push(current);
  return lines;
}

function renderPart(part: HudPart): ReactNode {
  return withUniqueKeys(part, segmentKey).map(({ item: segment, key }) => (
    <Text key={key} color={segment.color} bold={segment.bold}>
      {segment.text}
    </Text>
  ));
}

function HudLine({ parts, separator = " │ " }: { parts: HudPart[]; separator?: string }) {
  return (
    <Box>
      {withUniqueKeys(parts, partKey).map(({ item: part, key }, index) => (
        <Fragment key={key}>
          {index > 0 ? <Text color={BORDER}>{separator}</Text> : null}
          {renderPart(part)}
        </Fragment>
      ))}
    </Box>
  );
}

function execGit(args: string[], cwd: string): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile("git", args, { cwd, encoding: "utf8", timeout: 500 }, (error, stdout) => {
      if (error) reject(error);
      else resolve(stdout);
    });
  });
}

async function readGitBranch(cwd: string): Promise<string | null> {
  try {
    const branch = (await execGit(["rev-parse", "--abbrev-ref", "HEAD"], cwd)).trim();
    if (branch && branch !== "HEAD") return branch;

    const commit = (await execGit(["rev-parse", "--short", "HEAD"], cwd)).trim();
    return commit ? `detached@${commit}` : null;
  } catch {
    return null;
  }
}

function useGitBranch(cwd: string): string | null {
  const [branch, setBranch] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const refresh = () => {
      void readGitBranch(cwd).then((next) => {
        if (alive) setBranch(next);
      });
    };
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [cwd]);

  return branch;
}

export const MEMORY_POLL_INTERVAL_MS = 10_000;

/**
 * Poll deferred memory maintenance.
 *
 * Compilation and dreaming are background tasks that outlive the turn that
 * scheduled them, so they cannot ride a per-run SSE stream. They are minute-
 * scale, so a coarse poll is enough; a failed probe reports nothing rather than
 * an error, because an indicator must never be the reason a shell looks broken.
 */
function useMemoryMaintenance(baseUrl: string): MemoryMaintenance | null {
  const [maintenance, setMaintenance] = useState<MemoryMaintenance | null>(null);

  useEffect(() => {
    if (!baseUrl) return;
    let alive = true;
    const refresh = () => {
      void fetchMemoryMaintenance(baseUrl).then(
        (next) => {
          if (alive) setMaintenance(next);
        },
        () => {
          if (alive) setMaintenance(null);
        },
      );
    };
    refresh();
    const timer = setInterval(refresh, MEMORY_POLL_INTERVAL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [baseUrl]);

  return maintenance;
}

/**
 * The label for ambient memory work, or null when there is nothing to show.
 *
 * Running work outranks owed work. Dreaming is the rarest, then periodic
 * curation, then ordinary compilation, so the most meaningful active pass is
 * the one named.
 */
export function memoryMaintenanceLabel(maintenance: MemoryMaintenance | null): string | null {
  if (!maintenance) return null;
  if (maintenance.dream === "running") return "dreaming";
  if (maintenance.nudge === "running") return "curating memory";
  if (maintenance.compile === "running") return "compiling memory";
  if (maintenance.dream === "pending") return "dream queued";
  if (maintenance.nudge === "pending") return "memory review queued";
  if (maintenance.compile === "pending") return "memory queued";
  return null;
}

function segmentKey(segment: HudSegment): string {
  return [segment.text, segment.color ?? "", segment.bold ? "bold" : ""].join(":");
}

function partKey(part: HudPart): string {
  return part.map(segmentKey).join("|");
}

function lineKey(line: HudPart[]): string {
  return line.map(partKey).join("||");
}

function withUniqueKeys<T>(items: T[], getKey: (item: T) => string): Array<{ item: T; key: string }> {
  const seen = new Map<string, number>();
  return items.map((item) => {
    const base = getKey(item) || "item";
    const count = seen.get(base) ?? 0;
    seen.set(base, count + 1);
    return { item, key: count === 0 ? base : `${base}:${count}` };
  });
}

function runningToolParts(running: Record<string, string>): HudPart[] {
  const names = Object.values(running);
  return names.slice(-2).map((name) => [{ text: "◐", color: WARNING }, { text: " " }, { text: name, color: SESSION }]);
}

function countedToolParts(counts: Record<string, number>, marker: string, color: string, limit: number): HudPart[] {
  return Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit)
    .map<HudPart>(([name, count]) => [
      { text: marker, color },
      { text: " " },
      { text: name },
      { text: ` ×${count}`, color: MUTED },
    ]);
}

function memoryParts(maintenance: MemoryMaintenance | null): HudPart[] {
  const label = memoryMaintenanceLabel(maintenance);
  if (!label) return [];
  // Ambient, not an activity: memory work belongs to the agent, not to this
  // turn, so it reads as a dim aside rather than a tool result.
  return [[{ text: `☾ ${label}`, color: MUTED }]];
}

function collectToolParts(activity: ToolActivity, maintenance: MemoryMaintenance | null): HudPart[] {
  return [
    ...runningToolParts(activity.running),
    ...countedToolParts(activity.successes, "✓", SUCCESS, 4),
    ...countedToolParts(activity.blocked, "⛔", WARNING, 2),
    ...countedToolParts(activity.preflight, "◆", WARNING, 2),
    ...countedToolParts(activity.errors, "!", ERROR, 2),
    ...memoryParts(maintenance),
  ];
}

function buildHeaderParts(options: {
  model: string;
  projectName: string;
  gitBranch: string | null;
  sessionId?: string;
  turnCount: number;
  viewMode: TranscriptViewMode;
  tokenUsage: TokenUsage;
  contextUsage: ContextUsage;
}): HudPart[] {
  const contextPercent = Math.max(0, Math.min(100, Math.round(options.contextUsage.context_percent)));
  const parts: HudPart[] = [
    [{ text: `[${options.model || "-"}]`, color: MODEL }],
    [
      { text: "context ", color: MUTED },
      { text: `${contextPercent}%`, color: WARNING },
    ],
    [{ text: options.projectName || "-", color: PROJECT }],
  ];

  if (options.gitBranch) {
    parts.push([
      { text: "git ", color: MUTED },
      { text: options.gitBranch, color: GIT },
    ]);
  }

  parts.push(
    [
      { text: "session ", color: MUTED },
      { text: options.sessionId || "new", color: SESSION },
    ],
    [{ text: `${options.turnCount} turn${options.turnCount === 1 ? "" : "s"}`, color: MUTED }],
    [{ text: options.viewMode, color: MUTED }],
  );

  if (options.tokenUsage.total_tokens > 0) {
    parts.push([
      { text: "session tok ", color: MUTED },
      { text: formatTokenCount(options.tokenUsage.total_tokens), color: WARNING },
    ]);
  }

  return parts;
}

export const StatusHud = memo(function StatusHud(options: {
  model: string;
  projectName: string;
  cwd: string;
  baseUrl: string;
  sessionId?: string;
  turnCount: number;
  viewMode: TranscriptViewMode;
  tokenUsage: TokenUsage;
  contextUsage: ContextUsage;
  toolActivity: ToolActivity;
}) {
  const { columns } = useWindowSize();
  const gitBranch = useGitBranch(options.cwd);
  const maintenance = useMemoryMaintenance(options.baseUrl);
  const maxWidth = Math.max(1, columns - 4);
  const headerLines = wrapParts(buildHeaderParts({ ...options, gitBranch }), maxWidth);
  const activityLines = wrapParts(collectToolParts(options.toolActivity, maintenance), maxWidth);

  return (
    <Box flexDirection="column">
      {withUniqueKeys(headerLines, lineKey).map(({ item: line, key }) => (
        <HudLine key={`header-${key}`} parts={line} />
      ))}
      {withUniqueKeys(activityLines, lineKey).map(({ item: line, key }) => (
        <HudLine key={`activity-${key}`} parts={line} separator=" | " />
      ))}
    </Box>
  );
});

function formatTokenCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}m`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
}

export function formatElapsed(startedAt: number | null, now = Date.now()): string {
  if (startedAt === null) return "0s";
  const seconds = Math.max(0, Math.floor((now - startedAt) / 1_000));
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  return minutes > 0 ? `${minutes}m ${remainingSeconds}s` : `${remainingSeconds}s`;
}

export function buildRunProgressParts(
  startedAt: number | null,
  tokenUsage: TokenUsage,
  now = Date.now(),
): HudSegment[] {
  const parts: HudSegment[] = [
    { text: "  ", color: MUTED },
    { text: "working ", color: MUTED },
    { text: `(${formatElapsed(startedAt, now)}`, color: MUTED },
  ];

  if (tokenUsage.total_tokens > 0) {
    parts.push(
      { text: " · ↓ ", color: MUTED },
      { text: `${formatTokenCount(tokenUsage.total_tokens)} tokens`, color: MUTED },
    );
  }

  parts.push({ text: ")", color: MUTED });
  return parts;
}

export function RunProgress({ startedAt, tokenUsage }: { startedAt: number | null; tokenUsage: TokenUsage }) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, []);

  return (
    <Box>
      {buildRunProgressParts(startedAt, tokenUsage, now).map((part) => (
        <Text key={segmentKey(part)} color={part.color}>
          {part.text}
        </Text>
      ))}
    </Box>
  );
}
