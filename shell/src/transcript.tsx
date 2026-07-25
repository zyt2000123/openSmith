/** Transcript rendering — state shapes and transitions live in transcript-state.ts. */

import { MarkdownText } from "@assistant-ui/react-ink-markdown";
import { Box, Text, useWindowSize } from "ink";
import { useEffect, useState } from "react";

import type { ToolState } from "./activity.js";
import { CodeBlock, type CodeHighlighter } from "./code-block.js";
import { DiffBlock } from "./diff-block.js";
import { splitMarkdownLayoutBlocks } from "./markdown-layout.js";
import { type MarkdownSegment, splitMarkdownBlocks } from "./markdown-segments.js";
import { MarkdownTableBlock } from "./markdown-table.js";
import { stripEmojiIcons } from "./output.js";
import { skillPresentation } from "./skill-presentation.js";
import { SmithUiBlock as SmithUiView } from "./smith-ui.js";
import { splitStreamingMarkdown } from "./streaming-markdown.js";
import { truncateDisplay } from "./text-layout.js";
import { ACCENT, ASSISTANT, BORDER, ERROR, INFO, MUTED, SKILL, SUCCESS, WARNING } from "./theme.js";
import type {
  SkillBlock,
  SmithUiBlock,
  SmithUiFallbackBlock,
  SystemEntry,
  ThinkingBlock,
  ToolBlock,
  TranscriptEntry,
  TranscriptViewMode,
  TurnBlock,
  TurnEntry,
} from "./transcript-state.js";

const TOOL_PRESENTATION: Record<ToolState, { color: string; marker: string; label: string }> = {
  running: { color: WARNING, marker: "◐", label: "running" },
  success: { color: SUCCESS, marker: "●", label: "success" },
  error: { color: ERROR, marker: "✕", label: "error" },
  blocked: { color: WARNING, marker: "⛔", label: "permission blocked" },
  preflight: { color: WARNING, marker: "◆", label: "fact preflight" },
};

const MARKDOWN_OPTIONS = {
  theme: { listMarker: { color: ASSISTANT } },
  listIndent: 1,
} as const;

const PROCESSING_LABEL = "Processing";
const PROCESSING_SCAN_INTERVAL_MS = 110;

export type ProcessingScanSegment = {
  id: string;
  text: string;
  active: boolean;
};

/** Returns one left-to-right highlight frame for the active reply placeholder. */
export function processingScanSegments(frame: number): ProcessingScanSegment[] {
  const activeIndex = ((frame % PROCESSING_LABEL.length) + PROCESSING_LABEL.length) % PROCESSING_LABEL.length;

  return [...PROCESSING_LABEL].map((text, index) => ({ id: String(index), text, active: index === activeIndex }));
}

export function userMessageBoxProps(columns: number) {
  return {
    width: Math.max(1, columns - 4),
    borderColor: BORDER,
    borderStyle: "round",
    paddingX: 1,
  } as const;
}

function MarkdownContent({ text, width }: { text: string; width: number }) {
  const layoutBlocks = splitMarkdownLayoutBlocks(text);
  const blockKeyCounts = new Map<string, number>();

  return (
    <Box flexDirection="column">
      {layoutBlocks.map((block) => {
        const blockBaseKey = `${block.kind}-${block.text}`;
        const blockOccurrence = blockKeyCounts.get(blockBaseKey) ?? 0;
        blockKeyCounts.set(blockBaseKey, blockOccurrence + 1);
        return (
          <Box key={`${blockBaseKey}-${blockOccurrence}`}>
            {block.kind === "table" ? (
              <MarkdownTableBlock markdown={block.text} width={width} />
            ) : (
              <MarkdownText text={block.text} width={width} {...MARKDOWN_OPTIONS} />
            )}
          </Box>
        );
      })}
    </Box>
  );
}

function startsWithMarkdownHeading(text: string): boolean {
  return /^(?:[ \t]*\r?\n)*[ \t]{0,3}#{1,6}(?:\s|$)/u.test(text);
}

function DiffSegment({
  segment,
  highlighter,
  width,
}: {
  segment: Extract<MarkdownSegment, { type: "diff" }>;
  highlighter?: CodeHighlighter;
  width: number;
}) {
  return <DiffBlock source={segment.text} width={width} highlighter={highlighter} />;
}

function MarkdownSegmentView({
  segment,
  highlighter,
  width,
}: {
  segment: MarkdownSegment;
  highlighter?: CodeHighlighter;
  width: number;
}) {
  if (segment.type === "markdown") return segment.text ? <MarkdownContent text={segment.text} width={width} /> : null;
  if (segment.type === "diff") return <DiffSegment segment={segment} highlighter={highlighter} width={width} />;
  return <CodeBlock code={segment.text} language={segment.language} highlighter={highlighter} />;
}

function MarkdownDocument({
  text,
  highlighter,
  width,
}: {
  text: string;
  highlighter?: CodeHighlighter;
  width: number;
}) {
  const segments = splitMarkdownBlocks(text);
  const keyCounts = new Map<string, number>();
  return (
    <>
      {segments.map((segment) => {
        const baseKey = `${segment.type}-${segment.type === "code" ? segment.language : ""}-${segment.text}`;
        const occurrence = keyCounts.get(baseKey) ?? 0;
        keyCounts.set(baseKey, occurrence + 1);
        const key = `${baseKey}-${occurrence}`;
        return <MarkdownSegmentView key={key} segment={segment} highlighter={highlighter} width={width} />;
      })}
    </>
  );
}

function MarkdownMessage({
  text,
  highlighter,
  streaming = false,
  width,
}: {
  text: string;
  highlighter?: CodeHighlighter;
  streaming?: boolean;
  width: number;
}) {
  const snapshot = splitStreamingMarkdown(text, streaming);
  return (
    <>
      {snapshot.stable ? <MarkdownDocument text={snapshot.stable} highlighter={highlighter} width={width} /> : null}
      {snapshot.pending ? <MarkdownDocument text={snapshot.pending} highlighter={highlighter} width={width} /> : null}
    </>
  );
}

type ToolGroupBlock = {
  id: string;
  type: "tool_group";
  name: string;
  items: ToolBlock[];
};
type ToolSummaryBlock = {
  id: string;
  type: "tool_summary";
  counts: Record<string, number>;
};
type RenderBlock =
  | ThinkingBlock
  | ToolBlock
  | SkillBlock
  | SmithUiBlock
  | SmithUiFallbackBlock
  | ToolGroupBlock
  | ToolSummaryBlock;

function truncate(text: string, max = 80): string {
  // `max` is a column budget, so it has to be measured in terminal cells: UTF-16
  // length counts a CJK glyph as 1 (it occupies 2) and splits emoji surrogate pairs.
  return truncateDisplay(text, max);
}

function truncateLines(text: string, max = 4): { text: string; hidden: number } {
  const lines = text.split("\n");
  if (lines.length <= max) {
    return { text, hidden: 0 };
  }
  return {
    text: lines.slice(0, max).join("\n"),
    hidden: lines.length - max,
  };
}

function SystemMessage({
  entry,
  highlighter,
  width,
}: {
  entry: SystemEntry;
  highlighter?: CodeHighlighter;
  width: number;
}) {
  const trimmed = entry.text.trim();
  return (
    <Box marginBottom={1} paddingLeft={1}>
      {trimmed ? (
        <MarkdownMessage text={trimmed} highlighter={highlighter} width={width} />
      ) : (
        <Text color={entry.tone === "error" ? ERROR : MUTED}>{entry.text}</Text>
      )}
    </Box>
  );
}

function ThinkingMessage({ block, viewMode }: { block: ThinkingBlock; viewMode: TranscriptViewMode }) {
  const { text, hidden } = truncateLines(block.text, viewMode === "transcript" ? 5 : 2);
  return (
    <Box flexDirection="column" marginTop={1} paddingLeft={2}>
      <Text color={MUTED} italic>
        {block.done ? "∴ thinking" : "∴ thinking..."}
      </Text>
      <Text dimColor>{text || "working..."}</Text>
      {hidden > 0 ? (
        <Text color={BORDER}>
          … {hidden} more line{hidden === 1 ? "" : "s"}
        </Text>
      ) : null}
    </Box>
  );
}

function groupToolBlocks(blocks: TurnBlock[], viewMode: TranscriptViewMode): RenderBlock[] {
  if (viewMode === "transcript") {
    return blocks;
  }

  const grouped: RenderBlock[] = [];
  for (const block of blocks) {
    const previous = grouped[grouped.length - 1];
    if (block.type === "tool" && previous?.type === "tool_group" && previous.name === block.name) {
      previous.items.push(block);
      continue;
    }

    if (block.type === "tool" && previous?.type === "tool" && previous.name === block.name) {
      grouped[grouped.length - 1] = {
        id: previous.id,
        type: "tool_group",
        name: block.name,
        items: [previous, block],
      };
      continue;
    }

    grouped.push(block);
  }
  return grouped;
}

function recordSuccess(counts: Record<string, number>, item: ToolBlock): void {
  counts[item.name] = (counts[item.name] ?? 0) + 1;
}

function collapseBlock(block: RenderBlock, counts: Record<string, number>, kept: RenderBlock[]): boolean {
  if (block.type === "tool" && block.state === "success") {
    recordSuccess(counts, block);
    return true;
  }
  if (block.type === "tool_group" && block.items.every((item) => item.state === "success")) {
    for (const item of block.items) {
      recordSuccess(counts, item);
    }
    return true;
  }
  if (block.type === "thinking" && block.done) return true;

  kept.push(block);
  return false;
}

function collapseCompletedTools(blocks: RenderBlock[]): RenderBlock[] {
  const counts: Record<string, number> = {};
  const kept: RenderBlock[] = [];
  let collapsed = 0;

  for (const block of blocks) {
    if (collapseBlock(block, counts, kept)) collapsed++;
  }

  if (collapsed < 2) return blocks;
  if (Object.keys(counts).length === 0) return kept;

  const summary: ToolSummaryBlock = { id: "tool-summary", type: "tool_summary", counts };
  return [summary, ...kept];
}

function ToolSummaryMessage({ block }: { block: ToolSummaryBlock }) {
  const parts = Object.entries(block.counts).map(([name, count]) => `${count}x ${name}`);
  return (
    <Box marginTop={1} paddingLeft={2}>
      <Text color={SUCCESS}>✓ </Text>
      <Text color={MUTED}>{parts.join("  ")}</Text>
    </Box>
  );
}

function SmithUiFallbackMessage({ block }: { block: SmithUiFallbackBlock }) {
  return (
    <Box flexDirection="column" marginTop={1}>
      <Text color={WARNING}>structured result fallback: {block.reason}</Text>
      <CodeBlock code={block.code} language="json" />
    </Box>
  );
}

function toolSummaryColor(state: ToolState): string {
  if (state === "error") return ERROR;
  if (state === "blocked" || state === "preflight") return WARNING;
  return MUTED;
}

function ToolMessage({ block, viewMode }: { block: ToolBlock; viewMode: TranscriptViewMode }) {
  const presentation = TOOL_PRESENTATION[block.state];
  const { text, hidden } = truncateLines(block.summary, viewMode === "transcript" ? 5 : 3);

  return (
    <Box flexDirection="column" marginTop={1} paddingLeft={2}>
      <Box>
        <Text color={presentation.color}>{presentation.marker} </Text>
        <Text color={presentation.color} bold>
          {block.name}
        </Text>
        {block.hint ? (
          <Text color={MUTED}>
            {" ("}
            {truncate(block.hint, 56)}
            {")"}
          </Text>
        ) : null}
        <Text color={MUTED}>
          {"  "}
          {presentation.label}
        </Text>
      </Box>
      {block.summary ? (
        <>
          <Text color={toolSummaryColor(block.state)}> {text}</Text>
          {hidden > 0 ? (
            <Text color={BORDER}>
              {" "}
              … {hidden} more line{hidden === 1 ? "" : "s"}
            </Text>
          ) : null}
        </>
      ) : block.state === "running" ? (
        <Text color={BORDER}> waiting for result...</Text>
      ) : null}
    </Box>
  );
}

function ToolGroupMessage({ group }: { group: ToolGroupBlock }) {
  const successCount = group.items.filter((item) => item.state === "success").length;
  const blockedCount = group.items.filter((item) => item.state === "blocked").length;
  const preflightCount = group.items.filter((item) => item.state === "preflight").length;
  const errorCount = group.items.filter((item) => item.state === "error").length;
  const runningCount = group.items.filter((item) => item.state === "running").length;
  const lastHint = group.items[group.items.length - 1]?.hint || group.items[0]?.hint || "";
  const headline = [
    successCount ? `${successCount} ok` : "",
    blockedCount ? `${blockedCount} blocked` : "",
    preflightCount ? `${preflightCount} preflight` : "",
    errorCount ? `${errorCount} error` : "",
    runningCount ? `${runningCount} running` : "",
  ]
    .filter(Boolean)
    .join(", ");

  return (
    <Box flexDirection="column" marginTop={1} paddingLeft={2}>
      <Box>
        <Text color={INFO}>◦ </Text>
        <Text color={INFO} bold>
          {group.items.length}x {group.name}
        </Text>
        {lastHint ? (
          <Text color={MUTED}>
            {" ("}
            {truncate(lastHint, 48)}
            {")"}
          </Text>
        ) : null}
      </Box>
      <Text color={MUTED}> {headline || "grouped tool activity"}</Text>
    </Box>
  );
}

const SKILL_AGENT_NAMES: Record<string, string> = {
  understanding: "Explore",
  planning: "Plan",
  architecture: "Architect",
  implementation: "Implement",
  validation: "Verify",
};

function agentName(skillName: string): string {
  return SKILL_AGENT_NAMES[skillName] ?? skillName;
}

function actionName(name: string): string {
  const normalized = name.replace(/[_-]+/g, " ").trim();
  if (!normalized) return "Run tool";
  return normalized.replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function SkillActivityMessage({
  activity,
  viewMode,
}: {
  activity: SkillBlock["activities"][number];
  viewMode: TranscriptViewMode;
}) {
  const presentation = TOOL_PRESENTATION[activity.state];
  const { text, hidden } = truncateLines(activity.summary, viewMode === "transcript" ? 3 : 1);

  return (
    <Box flexDirection="column" paddingLeft={2}>
      <Box>
        <Text color={presentation.color}>{presentation.marker} </Text>
        <Text color={INFO}>{actionName(activity.name)}</Text>
        {activity.hint ? <Text color={MUTED}> {truncate(activity.hint, 64)}</Text> : null}
        <Text color={MUTED}> {presentation.label}</Text>
      </Box>
      {text ? <Text color={toolSummaryColor(activity.state)}> {text}</Text> : null}
      {hidden > 0 ? (
        <Text color={BORDER}>
          {" "}
          … {hidden} more line{hidden === 1 ? "" : "s"}
        </Text>
      ) : null}
    </Box>
  );
}

function SkillMessage({ block, viewMode }: { block: SkillBlock; viewMode: TranscriptViewMode }) {
  const presentation = skillPresentation(block.state);
  const color = presentation.tone === "error" ? ERROR : presentation.tone === "warning" ? WARNING : SUCCESS;
  const active = block.state === "running" || block.state === "retry";
  const activities = viewMode === "compact" ? block.activities.slice(-1) : block.activities;
  const hiddenCount = Math.max(0, block.activities.length - activities.length);

  return (
    <Box borderColor={color} borderStyle="round" flexDirection="column" marginTop={1} paddingX={1}>
      <Box>
        <Text color={color}>{active ? "◉ " : "● "}</Text>
        <Text color={INFO} bold>
          {presentation.heading}
        </Text>
        <Text color={MUTED}>{viewMode === "compact" ? " (workflow step · Ctrl+O to expand)" : " (workflow step)"}</Text>
      </Box>
      <Box paddingLeft={1}>
        <Text color={SKILL} bold>
          {agentName(block.name)}
        </Text>
        <Text color={MUTED}> · {block.name}</Text>
      </Box>
      {activities.length > 0 ? (
        activities.map((activity) => <SkillActivityMessage key={activity.id} activity={activity} viewMode={viewMode} />)
      ) : (
        <Text color={BORDER}> waiting for the first action…</Text>
      )}
      {hiddenCount > 0 ? (
        <Text color={BORDER}>
          {" "}
          … {hiddenCount} earlier action{hiddenCount === 1 ? "" : "s"}
        </Text>
      ) : null}
    </Box>
  );
}

function useBlink(active: boolean): boolean {
  const [visible, setVisible] = useState(true);

  useEffect(() => {
    if (!active) {
      setVisible(true);
      return;
    }

    const timer = setInterval(() => setVisible((current) => !current), 1_000);
    return () => clearInterval(timer);
  }, [active]);

  return visible;
}

function AssistantMarker({ active }: { active: boolean }) {
  const visible = useBlink(active);

  return (
    <Text color={WARNING} bold>
      {visible ? "● " : "  "}
    </Text>
  );
}

function ProcessingIndicator() {
  const [scanFrame, setScanFrame] = useState(0);
  const segments = processingScanSegments(scanFrame);

  useEffect(() => {
    const timer = setInterval(() => setScanFrame((frame) => frame + 1), PROCESSING_SCAN_INTERVAL_MS);
    return () => clearInterval(timer);
  }, []);

  return (
    <Box>
      {segments.map((segment) => (
        <Text key={segment.id} color={WARNING} bold={segment.active} dimColor={!segment.active}>
          {segment.text}
        </Text>
      ))}
    </Box>
  );
}

function TurnView({
  entry,
  viewMode,
  highlighter,
  terminalColumns,
}: {
  entry: TurnEntry;
  viewMode: TranscriptViewMode;
  highlighter?: CodeHighlighter;
  terminalColumns: number;
}) {
  const columns = terminalColumns;
  const markdownWidth = Math.max(1, columns - 4);
  const hasAssistantBody = entry.assistantText.trim().length > 0;
  const assistantBody = hasAssistantBody ? stripEmojiIcons(entry.assistantText).trimEnd() : "";
  const assistantStartsWithHeading = startsWithMarkdownHeading(assistantBody);
  const provisionalBody = stripEmojiIcons(entry.provisional.map((item) => item.text).join(""));
  const hasProvisionalBody = provisionalBody.trim().length > 0;
  const grouped = groupToolBlocks(entry.blocks, viewMode);
  const renderBlocks = viewMode === "compact" ? collapseCompletedTools(grouped) : grouped;

  return (
    <Box flexDirection="column" marginBottom={1}>
      {entry.userText ? (
        <Box {...userMessageBoxProps(columns)}>
          <Text color={ACCENT}>❯ </Text>
          <Text>{entry.userText}</Text>
        </Box>
      ) : null}

      {renderBlocks.map((block) => {
        switch (block.type) {
          case "thinking":
            return <ThinkingMessage key={block.id} block={block} viewMode={viewMode} />;
          case "tool":
            return <ToolMessage key={block.id} block={block} viewMode={viewMode} />;
          case "tool_group":
            return <ToolGroupMessage key={block.id} group={block} />;
          case "tool_summary":
            return <ToolSummaryMessage key={block.id} block={block} />;
          case "skill":
            return <SkillMessage key={block.id} block={block} viewMode={viewMode} />;
          case "smith_ui":
            return <SmithUiView key={block.id} payload={block.payload} />;
          case "smith_ui_fallback":
            return <SmithUiFallbackMessage key={block.id} block={block} />;
          default:
            return null;
        }
      })}

      {hasAssistantBody || hasProvisionalBody || entry.streaming ? (
        <Box marginTop={1} paddingLeft={2}>
          <Box marginTop={assistantStartsWithHeading ? 1 : 0}>
            <AssistantMarker active={entry.streaming} />
          </Box>
          <Box flexDirection="column" flexGrow={1}>
            {hasAssistantBody ? (
              <MarkdownMessage
                text={assistantBody}
                highlighter={highlighter}
                streaming={entry.streaming}
                width={markdownWidth}
              />
            ) : null}
            {hasProvisionalBody ? (
              <Box flexDirection="column">
                <Text color={MUTED} italic>
                  draft…
                </Text>
                <MarkdownMessage text={provisionalBody} highlighter={highlighter} streaming width={markdownWidth} />
              </Box>
            ) : hasAssistantBody ? null : (
              <ProcessingIndicator />
            )}
          </Box>
        </Box>
      ) : null}
    </Box>
  );
}

/**
 * Renders one transcript entry. Completed entries go through Ink's <Static>
 * (rendered exactly once, then live in scrollback); the active streaming turn
 * renders in the dynamic region with the same component.
 */
export function TranscriptEntryView({
  entry,
  viewMode,
  highlighter,
  terminalColumns,
}: {
  entry: TranscriptEntry;
  viewMode: TranscriptViewMode;
  highlighter?: CodeHighlighter;
  /** Optional viewport injection for deterministic terminal-renderer tests. */
  terminalColumns?: number;
}) {
  const { columns: windowColumns } = useWindowSize();
  const columns = terminalColumns ?? windowColumns;
  if (entry.kind === "system") {
    return <SystemMessage entry={entry} highlighter={highlighter} width={Math.max(1, columns - 1)} />;
  }

  return (
    <Box flexDirection="column">
      <TurnView entry={entry} viewMode={viewMode} highlighter={highlighter} terminalColumns={columns} />
    </Box>
  );
}
