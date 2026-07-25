import assert from "node:assert/strict";
import test from "node:test";

import { layoutMarkdownTable, parseMarkdownTable, renderMarkdownTableLines } from "./markdown-table.js";
import { displayWidth } from "./text-layout.js";

test("a table with more graphemes than the argument limit still lays out", () => {
  // Column sizing must fold over the cells; spreading one argument per grapheme
  // into Math.max blows the stack somewhere past ~100k and takes the shell down.
  const rows = Array.from({ length: 500 }, () => `| ${"x".repeat(400)} |`).join("\n");
  const table = parseMarkdownTable(`| head |\n| --- |\n${rows}`);

  assert.ok(table);
  assert.doesNotThrow(() => layoutMarkdownTable(table, 80));
});

const LONG_CHECKSUM = `sha256:${"a".repeat(72)}`;

test("parses GFM tables through the parser instead of splitting escaped pipes", () => {
  const table = parseMarkdownTable("| Name | Value |\n| :--- | ---: |\n| `code` and **bold** | x\\|y |");

  assert.deepEqual(table, {
    headers: ["Name", "Value"],
    alignments: ["left", "right"],
    rows: [["code and bold", "x|y"]],
  });
});

test("keeps a narrow table as a wrapped grid without an ellipsis or vertical cards", () => {
  const table = parseMarkdownTable(
    `| Module | Current responsibility |\n| --- | --- |\n| execution | This sentence stays complete in a narrow terminal. |\n| checksum | ${LONG_CHECKSUM} |`,
  );
  assert.ok(table);

  const layout = layoutMarkdownTable(table, 40);
  const lines = renderMarkdownTableLines(layout);

  assert.equal(layout.overflowed, false);
  assert.match(lines[0] ?? "", /^┌/);
  assert.ok(lines.some((line) => line.startsWith("├")));
  assert.ok(lines.every((line) => displayWidth(line) <= 40));
  assert.equal(layout.rows[2]?.cells[1]?.lines.join(""), LONG_CHECKSUM);
  assert.doesNotMatch(lines.join("\n"), /…|Current responsibility:/);
});

test("records the structural overflow case instead of dropping content", () => {
  const table = parseMarkdownTable("| A | B | C |\n| --- | --- | --- |\n| long | value | content |");
  assert.ok(table);

  const layout = layoutMarkdownTable(table, 5);

  assert.equal(layout.overflowed, true);
  assert.equal(layout.rows[1]?.cells[0]?.lines.join(""), "long");
  assert.equal(layout.rows[1]?.cells[1]?.lines.join(""), "value");
  assert.equal(layout.rows[1]?.cells[2]?.lines.join(""), "content");
});

test("marks a CJK grid as overflowed when a complete wide grapheme cannot fit in every column", () => {
  const table = parseMarkdownTable(
    "| 一 | 二 | 三 | 四 | 五 |\n| --- | --- | --- | --- | --- |\n| 甲 | 乙 | 丙 | 丁 | 戊 |",
  );
  assert.ok(table);

  const layout = layoutMarkdownTable(table, 14);
  const lines = renderMarkdownTableLines(layout);

  assert.equal(layout.overflowed, true);
  assert.ok(lines.every((line) => displayWidth(line) <= layout.width));
  assert.match(lines.join(""), /甲.*乙.*丙.*丁.*戊/);
});
