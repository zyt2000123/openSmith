import assert from "node:assert/strict";
import test from "node:test";

import { splitMarkdownLayoutBlocks } from "./markdown-layout.js";

test("keeps headings with content while separating tables for terminal layout", () => {
  assert.deepEqual(
    splitMarkdownLayoutBlocks("# 核心状态机\n\n说明文字\n\n| 变量 | 语义 |\n| --- | --- |\n| count | 轮次 |"),
    [
      { kind: "content", text: "# 核心状态机\n\n说明文字" },
      {
        kind: "table",
        text: "| 变量 | 语义 |\n| --- | --- |\n| count | 轮次 |",
      },
    ],
  );
});

test("keeps lists together and does not split headings inside fenced code", () => {
  const markdown = "- one\n\n- two\n\n```text\n# not a heading\n```";

  assert.deepEqual(splitMarkdownLayoutBlocks(markdown), [{ kind: "content", text: markdown }]);
});

test("does not treat a backtick-bearing info string as a fenced block", () => {
  const markdown = "``` `invalid`\n| A | B |\n| --- | --- |\n| one | two |\n```\nAfter";

  assert.deepEqual(splitMarkdownLayoutBlocks(markdown), [
    { kind: "content", text: "``` `invalid`" },
    { kind: "table", text: "| A | B |\n| --- | --- |\n| one | two |" },
    { kind: "content", text: "```\nAfter" },
  ]);
});
