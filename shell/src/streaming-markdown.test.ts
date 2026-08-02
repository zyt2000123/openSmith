import assert from "node:assert/strict";
import test from "node:test";

import { splitStreamingMarkdown } from "./streaming-markdown.js";

test("holds a streamed table tail until the table closes", () => {
  const snapshot = splitStreamingMarkdown("intro\n| A | B |\n| --- | --- |\n| one | tw", true);

  assert.equal(snapshot.stable, "intro\n");
  assert.equal(snapshot.pending, "| A | B |\n| --- | --- |\n| one | tw");
});

test("flushes a completed table before subsequent prose", () => {
  const snapshot = splitStreamingMarkdown("| A | B |\n| --- | --- |\n| one | two |\n\nnext\n", true);

  assert.equal(snapshot.stable, "| A | B |\n| --- | --- |\n| one | two |\n\nnext\n");
  assert.equal(snapshot.pending, "");
});

test("holds an unfinished fenced block and flushes it on completion", () => {
  const live = splitStreamingMarkdown("before\n```ts\nconst value = 1", true);
  const done = splitStreamingMarkdown("before\n```ts\nconst value = 1\n```\n", false);

  assert.equal(live.stable, "before\n");
  assert.equal(live.pending, "```ts\nconst value = 1");
  assert.equal(done.stable, "before\n```ts\nconst value = 1\n```\n");
  assert.equal(done.pending, "");
});

test("never drops source text while holding a streamed construct", () => {
  const samples = [
    "plain prose\nwith a partial tail",
    "| A | B |\n| --- | --- |\n| one | two |\n",
    "before\n```ts\nconst token = very_long_identifier",
  ];

  for (const source of samples) {
    const snapshot = splitStreamingMarkdown(source, true);
    assert.equal(snapshot.stable + snapshot.pending, source);
  }
});

test("does not hold a malformed backtick fence as an unfinished code block", () => {
  const source = "``` `invalid`\nnext\n";

  assert.deepEqual(splitStreamingMarkdown(source, true), { stable: source, pending: "" });
});
