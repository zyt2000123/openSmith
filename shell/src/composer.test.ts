import assert from "node:assert/strict";
import test from "node:test";
import type { Key } from "ink";

import { applyComposerEdit } from "./composer.js";

test("Backspace removes one Unicode grapheme without leaving a broken surrogate", () => {
  const result = applyComposerEdit("A👨‍👩‍👧‍👦B", 2, "", { backspace: true } as Key);

  assert.deepEqual(result, { value: "AB", cursor: 1 });
  assert.equal(result.value.includes("\uFFFD"), false);
});

test("bulk pasted text is ignored", () => {
  const result = applyComposerEdit("AB", 1, "😀e\u0301", {} as Key);

  assert.deepEqual(result, { value: "AB", cursor: 1 });
});

test("a pasted line break is ignored", () => {
  const result = applyComposerEdit("AB", 1, "\r", {} as Key);

  assert.deepEqual(result, { value: "AB", cursor: 1 });
});
