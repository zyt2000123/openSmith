import assert from "node:assert/strict";
import test from "node:test";
import { Box, renderToString } from "ink";
import stringWidth from "string-width";

import { skillPresentation } from "./skill-presentation.js";
import { BORDER } from "./theme.js";
import { processingScanSegments, TranscriptEntryView, userMessageBoxProps } from "./transcript.js";
import { createTurnEntry } from "./transcript-state.js";

function completedTurn(userText: string) {
  return { ...createTurnEntry(userText), streaming: false };
}

function stripAnsi(text: string): string {
  const ansiEscape = String.fromCharCode(27);
  return text.replace(new RegExp(`${ansiEscape}\\[[0-?]*[ -/]*[@-~]`, "g"), "");
}

test("workflow cards expose non-success terminal labels without mounting Ink", () => {
  assert.deepEqual(skillPresentation("running"), { heading: "Running Agent...", tone: "warning" });
  assert.deepEqual(skillPresentation("retry"), { heading: "Retrying Agent...", tone: "warning" });
  assert.deepEqual(skillPresentation("blocked"), { heading: "Agent blocked", tone: "warning" });
  assert.deepEqual(skillPresentation("error"), { heading: "Agent failed", tone: "error" });
  assert.deepEqual(skillPresentation("done"), { heading: "Agent complete", tone: "success" });
});

test("transcript turns frame user messages while aligning their content with replies", () => {
  const entry = { ...completedTurn("hello"), assistantText: "hi there" };
  const output = renderToString(<TranscriptEntryView entry={entry} viewMode="compact" />);

  assert.match(output, /hello/);
  assert.match(output, /hi there/);
  assert.match(output, /╭/);
  assert.match(output, /╰/);
  const lines = stripAnsi(output).split("\n");
  const userLine = lines.find((line) => line.includes("hello"));
  const assistantLine = lines.find((line) => line.includes("hi there"));
  assert.ok(userLine);
  assert.ok(assistantLine);
  assert.equal(userLine.indexOf("❯"), assistantLine.indexOf("●"));
  assert.equal(userLine.indexOf("hello"), assistantLine.indexOf("hi there"));
  assert.deepEqual(userMessageBoxProps(80), {
    width: 76,
    borderColor: BORDER,
    borderStyle: "round",
    paddingX: 1,
  });
  assert.equal(userMessageBoxProps(2).width, 1);
});

test("an over-long tool hint is truncated on grapheme boundaries", () => {
  const toolBlock = (hint: string) => ({
    id: "tool-1",
    type: "tool" as const,
    toolCallId: "call-1",
    name: "read",
    hint,
    state: "success" as const,
    summary: "",
  });
  const renderHint = (hint: string) =>
    stripAnsi(
      renderToString(
        <TranscriptEntryView
          entry={{ ...completedTurn("check config"), blocks: [toolBlock(hint)] }}
          viewMode="transcript"
        />,
      ),
    );

  // A 40-emoji hint is 80 UTF-16 units, so slicing at 55 lands mid-surrogate and
  // emits a lone half. Measuring cells instead cuts on a grapheme boundary.
  const loneSurrogate = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;
  assert.equal(loneSurrogate.test(renderHint("🎉".repeat(40))), false);
});

test("transcript keeps the assistant marker on the first Markdown heading line", () => {
  const entry = { ...completedTurn("Explain memory"), assistantText: "## Agent 记忆系统的核心关注点" };
  const output = stripAnsi(renderToString(<TranscriptEntryView entry={entry} viewMode="compact" />));
  const headingLine = output.split("\n").find((line) => line.includes("Agent 记忆系统的核心关注点"));

  assert.ok(headingLine);
  assert.match(headingLine, /●\s+Agent 记忆系统的核心关注点/);
});

test("processing placeholder sweeps its bright character from left to right", () => {
  const firstFrame = processingScanSegments(0);
  const nextFrame = processingScanSegments(1);
  const finalFrame = processingScanSegments(9);

  assert.equal(firstFrame.map((segment) => segment.text).join(""), "Processing");
  assert.deepEqual(
    firstFrame.map((segment) => segment.active),
    [true, false, false, false, false, false, false, false, false, false],
  );
  assert.deepEqual(
    nextFrame.map((segment) => segment.active),
    [false, true, false, false, false, false, false, false, false, false],
  );
  assert.deepEqual(
    finalFrame.map((segment) => segment.active),
    [false, false, false, false, false, false, false, false, false, true],
  );
});

test("transcript keeps JSON payloads in the code renderer", () => {
  const entry = {
    ...completedTurn("Show an event"),
    assistantText: `\`\`\`json
{
  "event_id": "evt_123"
}
\`\`\``,
  };
  const output = stripAnsi(renderToString(<TranscriptEntryView entry={entry} viewMode="compact" />));

  assert.match(output, /\[json\] · 3 行/);
});

test("transcript renders an invalid smith-ui payload through CodeBlock", () => {
  const entry = {
    ...completedTurn("Show a component"),
    blocks: [
      {
        id: "ui-fallback",
        type: "smith_ui_fallback" as const,
        reason: "Unsupported smith-ui payload",
        code: '{\n  "type": "TextInput"\n}',
      },
    ],
  };
  const output = stripAnsi(renderToString(<TranscriptEntryView entry={entry} viewMode="compact" />));

  assert.match(output, /structured result fallback/);
  assert.match(output, /\[json\] · 3 行/);
});

test("transcript keeps ordinary text code in the code renderer", () => {
  const entry = {
    ...completedTurn("Show a command"),
    assistantText: `\`\`\`
curl --request POST https://example.test
\`\`\``,
  };
  const output = stripAnsi(renderToString(<TranscriptEntryView entry={entry} viewMode="compact" />));

  assert.match(output, /\[text\] · 1 行/);
});

test("transcript keeps every long table cell visible in a narrow terminal", () => {
  const detail = "这段表格内容必须完整保留，不能因为终端宽度不足而被省略号替代。";
  const checksum = `sha256:${"a".repeat(160)}`;
  const entry = {
    ...completedTurn("Show the full table"),
    assistantText: `| Module | 当前职责 |\n| --- | --- |\n| execution | ${detail} |\n| checksum | ${checksum} |`,
  };
  const output = stripAnsi(renderToString(<TranscriptEntryView entry={entry} viewMode="compact" />));
  const compactOutput = output.replace(/[\s│┌┐└┘├┤┬┴┼─]/g, "");

  assert.match(compactOutput, new RegExp(detail));
  assert.match(compactOutput, new RegExp(checksum));
  assert.doesNotMatch(output, /…/);
});

test("transcript keeps Markdown tables compact between headings", () => {
  const entry = {
    ...completedTurn("Explain memory layers"),
    assistantText: `## 1. 记忆分层

| 层级 | 生命周期 |
| --- | --- |
| 会话记忆 | 单次对话 |

## 2. 写什么`,
  };
  const lines = stripAnsi(renderToString(<TranscriptEntryView entry={entry} viewMode="compact" />)).split("\n");
  const firstHeading = lines.findIndex((line) => line.includes("1. 记忆分层"));
  const tableTop = lines.findIndex((line) => line.includes("┌"));
  const tableBottom = lines.findIndex((line, index) => index > tableTop && line.includes("└"));
  const secondHeading = lines.findIndex((line, index) => index > tableBottom && line.includes("2. 写什么"));

  assert.ok(firstHeading >= 0);
  assert.ok(tableTop > firstHeading);
  assert.ok(tableBottom > tableTop);
  assert.ok(secondHeading > tableBottom);
  assert.equal(lines.slice(firstHeading + 1, tableTop).filter((line) => !line.trim()).length, 1);
  assert.equal(lines.slice(tableBottom + 1, secondHeading).filter((line) => !line.trim()).length, 1);
});

test("transcript keeps ordinary code blocks compact between prose", () => {
  const entry = {
    ...completedTurn("Explain a session shape"),
    assistantText: `最朴素的形态是有序消息列表。\n\n\`\`\`\nSession {\n  session_id: "abc123"\n}\n\`\`\`\n\n继续说明会话级 context。`,
  };
  const lines = stripAnsi(renderToString(<TranscriptEntryView entry={entry} viewMode="compact" />)).split("\n");
  const prose = lines.findIndex((line) => line.includes("最朴素的形态"));
  const codeTop = lines.findIndex((line) => line.includes("┌"));
  const codeBottom = lines.findIndex((line, index) => index > codeTop && line.includes("└"));
  const trailing = lines.findIndex((line, index) => index > codeBottom && line.includes("继续说明会话级 context"));

  assert.ok(prose >= 0);
  assert.ok(codeTop > prose);
  assert.ok(codeBottom > codeTop);
  assert.ok(trailing > codeBottom);
  assert.equal(lines.slice(prose + 1, codeTop).filter((line) => !line.trim()).length, 1);
  assert.equal(lines.slice(codeBottom + 1, trailing).filter((line) => !line.trim()).length, 1);
});

test("transcript keeps an ordinary long Markdown token visible in a 40-column terminal", () => {
  const token = `ordinary_${"m".repeat(72)}`;
  const entry = {
    ...completedTurn("prose"),
    assistantText: `普通正文应该换行，但不能丢失 ${token}`,
  };
  const output = stripAnsi(
    renderToString(<TranscriptEntryView entry={entry} viewMode="compact" terminalColumns={40} />, { columns: 40 }),
  );
  const compact = output.replace(/\s/g, "");

  assert.ok(output.split("\n").every((line) => stringWidth(line) <= 40));
  assert.match(compact, new RegExp(token));
});

test("transcript keeps a full grid inside a 40-column terminal", () => {
  const checksum = `sha256:${"b".repeat(72)}`;
  const entry = {
    ...completedTurn("table"),
    assistantText: `| 模块 | 内容 |\n| --- | --- |\n| execution | 终端表格必须完整换行，不得省略。 |\n| checksum | ${checksum} |`,
  };
  const output = stripAnsi(
    renderToString(<TranscriptEntryView entry={entry} viewMode="compact" terminalColumns={40} />, { columns: 40 }),
  );
  const compact = output.replace(/[\s│┌┐└┘├┤┬┴┼─]/g, "");

  assert.ok(output.split("\n").every((line) => stringWidth(line) <= 40));
  assert.match(compact, new RegExp(checksum));
  assert.match(output, /┌.*┬.*┐/);
});

test("transcript keeps structured diffs inside a 40-column terminal", () => {
  const checksum = `sha256:${"d".repeat(72)}`;
  const entry = {
    ...completedTurn("diff"),
    assistantText: `\`\`\`diff\n@@ -1 +1 @@\n-const checksum = "old";\n+const checksum = "${checksum}";\n\`\`\``,
  };
  const output = stripAnsi(
    renderToString(<TranscriptEntryView entry={entry} viewMode="compact" terminalColumns={40} />, { columns: 40 }),
  );
  const compact = output.replace(/[\s│┌┐└┘├┤┬┴┼─]/g, "");

  assert.ok(output.split("\n").every((line) => stringWidth(line) <= 40));
  assert.match(compact, new RegExp(checksum));
});

test("transcript does not truncate a narrow fenced code block", () => {
  const token = `token_${"c".repeat(72)}`;
  const entry = {
    ...completedTurn("code"),
    assistantText: `\`\`\`typescript\nconst value = "${token}";\n\`\`\``,
  };
  const output = stripAnsi(
    renderToString(<TranscriptEntryView entry={entry} viewMode="compact" terminalColumns={40} />, { columns: 40 }),
  );
  const compact = output.replace(/[\s│┌┐└┘├┤┬┴┼─]/g, "");

  assert.ok(output.split("\n").every((line) => stringWidth(line) <= 40));
  assert.match(compact, new RegExp(token));
});

// The mount in index.tsx wraps every entry in `<Box paddingX={2}>`. Rendering the
// entry bare — as the assertions above do — hides four columns of indent, so a grid
// budgeted against the raw terminal width still looked correct in tests while Ink
// wrapped the trailing border cell of every row in the real shell.
function renderInTranscriptContainer(node: React.ReactNode, columns: number): string {
  return stripAnsi(
    renderToString(
      <Box flexDirection="column" paddingX={2}>
        {node}
      </Box>,
      { columns },
    ),
  );
}

test("tables keep their grid intact inside the padded transcript container", () => {
  const columns = 80;
  const entry = {
    ...completedTurn("film facts"),
    // The cell has to be long enough to claim the whole content budget: a narrow
    // grid fits either way and would assert nothing.
    assistantText: [
      "| 项目 | 内容 |",
      "| --- | --- |",
      "| 导演/编剧 | 克里斯托弗·诺兰 |",
      `| 主演 | ${"麦特·戴蒙、汤姆·霍兰德、安·海瑟薇、罗伯·派汀森、".repeat(4)} |`,
    ].join("\n"),
  };

  const lines = renderInTranscriptContainer(
    <TranscriptEntryView entry={entry} viewMode="compact" terminalColumns={columns} />,
    columns,
  ).split("\n");

  const top = lines.find((line) => line.includes("┌"));
  const bottom = lines.find((line) => line.includes("└"));

  assert.ok(top, "expected a table top border");
  assert.ok(bottom, "expected a table bottom border");
  // A wrapped grid line leaves its closing corner alone on the next row.
  assert.ok(top.includes("┐"), `top border wrapped: ${JSON.stringify(top)}`);
  assert.ok(bottom.includes("┘"), `bottom border wrapped: ${JSON.stringify(bottom)}`);
  assert.ok(lines.every((line) => stringWidth(line) <= columns));
});

test("system entries keep their body inside the padded transcript container", () => {
  const columns = 80;
  const entry = {
    id: "system-1",
    kind: "system" as const,
    text: ["| 键 | 值 |", "| --- | --- |", `| 模型 | ${"claude-opus-latest ".repeat(6)}|`].join("\n"),
    tone: "info" as const,
  };

  const lines = renderInTranscriptContainer(
    <TranscriptEntryView entry={entry} viewMode="compact" terminalColumns={columns} />,
    columns,
  ).split("\n");

  const top = lines.find((line) => line.includes("┌"));

  assert.ok(top, "expected a table top border");
  assert.ok(top.includes("┐"), `top border wrapped: ${JSON.stringify(top)}`);
  assert.ok(lines.every((line) => stringWidth(line) <= columns));
});

test("transcript renders fenced diffs with structured line gutters", () => {
  const entry = {
    ...completedTurn("Show the patch"),
    assistantText: "```diff\n@@ -1 +1 @@\n-const mode = 'old';\n+const mode = 'new';\n```",
  };
  const output = stripAnsi(renderToString(<TranscriptEntryView entry={entry} viewMode="compact" />));

  assert.match(output, /@@ -1 \+1 @@/);
  assert.match(output, /│ - const mode = 'old';/);
  assert.match(output, /│ \+ const mode = 'new';/);
});
