import assert from "node:assert/strict";
import test from "node:test";

import { splitMarkdownBlocks } from "./markdown-segments.js";

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

test("treats a Mermaid fence as an ordinary code block", () => {
  const segments = splitMarkdownBlocks("Before\n\n```mermaid\nflowchart TD\n  A[Start] --> B[End]\n```\n\nAfter");

  assert.deepEqual(segments, [
    { type: "markdown", text: "Before\n" },
    { type: "code", language: "mermaid", text: "flowchart TD\n  A[Start] --> B[End]" },
    { type: "markdown", text: "\nAfter" },
  ]);
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

test("keeps a malformed backtick fence in Markdown instead of swallowing following content", () => {
  const source = "``` `invalid`\n| A | B |\n| --- | --- |\n```\nAfter";

  assert.deepEqual(splitMarkdownBlocks(source), [
    { type: "markdown", text: "``` `invalid`\n| A | B |\n| --- | --- |" },
    { type: "markdown", text: "```\nAfter" },
  ]);
});
