import assert from "node:assert/strict";
import test from "node:test";
import stringWidth from "string-width";

import { renderMermaidDiagram, renderSimpleTextDiagram, splitMarkdownBlocks } from "./mermaid.js";

/** Diagram output carries ANSI colour; these assertions are about layout and text. */
function stripAnsi(value: string | null): string {
  // biome-ignore lint/suspicious/noControlCharactersInRegex: matching the ANSI escape is the point
  return (value ?? "").replace(/\u001b\[[0-9;]*m/g, "");
}

/** Layout/text assertions run against the uncoloured grid. */
function renderPlain(source: string): string {
  return stripAnsi(renderMermaidDiagram(source));
}

test("splits ordinary fenced code into a code segment", () => {
  assert.deepEqual(splitMarkdownBlocks("Before\n\n```python\nprint('hi')\n```\n\nAfter"), [
    { type: "markdown", text: "Before\n" },
    { type: "code", language: "python", text: "print('hi')" },
    { type: "markdown", text: "\nAfter" },
  ]);
});

test("splits fenced diff blocks into a dedicated structured-rendering segment", () => {
  assert.deepEqual(splitMarkdownBlocks("before\n```diff\n-old\n+new\n```\nafter"), [
    { type: "markdown", text: "before" },
    { type: "diff", language: "diff", text: "-old\n+new" },
    { type: "markdown", text: "after" },
  ]);
});

test("splits Mermaid fenced blocks from surrounding Markdown", () => {
  const segments = splitMarkdownBlocks("Before\n\n```mermaid\nflowchart TD\n  A[Start] --> B[End]\n```\n\nAfter");

  assert.deepEqual(segments, [
    { type: "markdown", text: "Before\n" },
    { type: "mermaid", text: "flowchart TD\n  A[Start] --> B[End]" },
    { type: "markdown", text: "\nAfter" },
  ]);
});

test("recognizes Mermaid language tags case-insensitively", () => {
  const segments = splitMarkdownBlocks("```Mermaid\ngraph LR\n  A --> B\n```");

  assert.deepEqual(segments, [{ type: "mermaid", text: "graph LR\n  A --> B" }]);
});

test("keeps an unfinished Mermaid fence as ordinary Markdown", () => {
  const source = "```mermaid\nflowchart TD\n  A --> B";

  assert.deepEqual(splitMarkdownBlocks(source), [{ type: "markdown", text: source }]);
});

test("keeps the language when a fence info string has extra words", () => {
  const segments = splitMarkdownBlocks("```ts twoslash\nconst x = 1;\n```\nDone.");

  assert.deepEqual(segments, [
    { type: "code", language: "ts", text: "const x = 1;" },
    { type: "markdown", text: "Done." },
  ]);
});

test("renders a basic Mermaid flowchart as terminal Unicode", () => {
  const rendered = renderPlain("flowchart TD\n  A[Start] --> B[End]");

  assert.match(rendered, /Start/);
  assert.match(rendered, /End/);
  assert.match(rendered, /[┌┐└┘]/);
  assert.doesNotMatch(rendered, /A\[Start\]/);
  assert.equal(
    rendered?.split("\n").some((line) => line.endsWith(" ")),
    false,
  );
});

test("renders Mermaid sequence diagrams as terminal Unicode", () => {
  const rendered = renderPlain("sequenceDiagram\n  云平台->>用户系统: HTTP POST");

  assert.match(rendered, /云平台/);
  assert.match(rendered, /用户系统/);
  assert.match(rendered, /HTTP POST/);
  assert.match(rendered, /[┌┐└┘]/);
});

test("turns an unlabelled two-endpoint arrow into a diagram", () => {
  const rendered = renderSimpleTextDiagram(`用户系统  <——HTTP POST——  云平台
                      (事件发生时)`);

  assert.match(rendered ?? "", /用户系统/);
  assert.match(rendered ?? "", /云平台/);
  assert.match(rendered ?? "", /HTTP.*POST/);
  assert.match(rendered ?? "", /事件发生时/);
  assert.match(rendered ?? "", /[┌┐└┘]/);
});

test("keeps ordinary text outside the simple-diagram grammar", () => {
  assert.equal(renderSimpleTextDiagram("curl --request POST https://example.test"), null);
});

test("turns HTML line breaks in node labels into readable terminal text", () => {
  const rendered = renderPlain('flowchart TD\n  A["save_conversation_memory()<br/>提取证据 → recent.jsonl"]');

  assert.ok(rendered);
  assert.doesNotMatch(rendered, /<br\s*\/?\s*>/i);
  assert.match(rendered, /save_conversation_memory\(\)/);
  assert.match(rendered, /提取证据.*recent\.jsonl/);
});

test("keeps CJK node labels inside their terminal box", () => {
  const rendered = renderPlain("flowchart TD\n  A[完整生命周期] --> B[对话结束]");

  assert.ok(rendered);
  const lines = rendered.split("\n");
  const border = lines.find((line) => line.startsWith("┌"));
  const label = lines.find((line) => line.includes("完整生命周期"));

  assert.ok(border);
  assert.ok(label);
  assert.equal(stringWidth(label), stringWidth(border));
});

test("preserves node identity when a CJK label is referenced more than once", () => {
  const rendered = renderPlain("flowchart TD\n  A[开始] --> B[中间]\n  B --> C[结束]");

  assert.ok(rendered);
  assert.equal(rendered.match(/中间/g)?.length, 1);
});

test("keeps CJK edge labels aligned with a horizontal branch", () => {
  const rendered = renderPlain("flowchart LR\n  A[开始] -->|是| B[结束]");

  assert.ok(rendered);
  const lines = rendered.split("\n");
  const border = lines.find((line) => line.startsWith("┌"));
  const label = lines.find((line) => line.includes("是"));

  assert.ok(border);
  assert.ok(label);
  assert.ok(stringWidth(label) <= stringWidth(border));
});

test("moves long edge labels below the diagram and wraps them", () => {
  const label =
    "关键设计决策： Markdown 是唯一事实源， SQLite 索引是可丢弃的派生物。索引损坏时自动重建，不影响记忆本身。";
  const rendered = renderPlain(`flowchart LR
  A[recent.jsonl] -->|${label}| B[agent/*.md]
  B --> C[project/*.md]
  C --> D[search.sqlite]`);

  assert.ok(rendered);
  assert.match(rendered, /↳ 关键设计决策： Markdown 是唯一事实源， SQLite 索引是可丢弃的派生物。索引损坏时/);
  assert.match(rendered, /自动重建，不影响记忆本身。/);
  assert.equal(
    rendered.split("\n").some((line) => line.includes("recent.jsonl") && line.includes("关键设计决策")),
    false,
  );
  assert.equal(
    rendered.split("\n").every((line) => stringWidth(line) <= 80),
    true,
  );
});

test("keeps the full memory lifecycle diagram readable", () => {
  const rendered = renderPlain(`flowchart TD
  A[完整生命周期] --> B[对话结束]
  B --> C[save_conversation_memory()<br/>提取证据 → recent.jsonl]
  C --> D{有学习信号或<br/>累计5个有效turn?}
  D -->|是| E[Compiler 生成草稿<br/>(LLM)]
  D -->|否| F{累计50个<br/>有效turn?}
  E --> G[Reviewer 审核<br/>(gate_llm，最多3轮)]
  G -->|通过| H[确定性代码校验<br/>结构 / 预算 / 安全]
  H -->|通过| I[原子写入<br/>备份旧文件 → os.replace]
  G -->|拒绝/超时| J[后续对话<br/>分层加载记忆]
  F -->|是| K[Dream 整理<br/>清洗 / 合并 / 压缩]
  F -->|否| J
  K --> J`);

  assert.ok(rendered);
  assert.doesNotMatch(rendered, /<br\s*\/?\s*>/i);
  for (const label of ["对话结束", "Compiler 生成草稿", "Dream 整理", "后续对话"]) {
    assert.match(rendered, new RegExp(label));
  }
});

test("returns null for an unsupported or invalid diagram", () => {
  assert.equal(renderMermaidDiagram("not valid Mermaid"), null);
});

test("colours the diagram frame, labels and arrows from the shell palette", () => {
  const rendered = renderMermaidDiagram("flowchart TD\n  A[Start] --> B[End]");

  assert.ok(rendered);
  // biome-ignore lint/suspicious/noControlCharactersInRegex: matching the ANSI escape is the point
  assert.match(rendered, /\u001b\[38;2;92;92;99m/); // BORDER on the frame
  // biome-ignore lint/suspicious/noControlCharactersInRegex: matching the ANSI escape is the point
  assert.match(rendered, /\u001b\[38;2;233;233;234m/); // INFO on the labels
  // biome-ignore lint/suspicious/noControlCharactersInRegex: matching the ANSI escape is the point
  assert.match(rendered, /\u001b\[38;2;97;175;239m/); // ASSISTANT on the arrowheads
  assert.equal(stripAnsi(rendered).includes("\u001b"), false);
});
