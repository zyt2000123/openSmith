import assert from "node:assert/strict";
import test from "node:test";
import type { Key } from "ink";

import { applyComposerEdit } from "./composer.js";

const ESC = String.fromCharCode(27);

test("Backspace removes one Unicode grapheme without leaving a broken surrogate", () => {
  const result = applyComposerEdit("A👨‍👩‍👧‍👦B", 2, "", { backspace: true } as Key);

  assert.deepEqual(result, { value: "AB", cursor: 1 });
  assert.equal(result.value.includes("\uFFFD"), false);
});

test("a pasted chunk is inserted at the cursor", () => {
  const result = applyComposerEdit("AB", 1, "paste", {} as Key);

  assert.deepEqual(result, { value: "ApasteB", cursor: 6 });
});

test("an IME word commit arrives as one chunk and still reaches the value", () => {
  const result = applyComposerEdit("", 0, "你好", {} as Key);

  assert.deepEqual(result, { value: "你好", cursor: 2 });
});

test("a pasted line break folds into a space so the single-line composer survives", () => {
  const result = applyComposerEdit("AB", 1, "x\r\ny", {} as Key);

  assert.deepEqual(result, { value: "Ax yB", cursor: 4 });
});

test("pasted terminal colouring never reaches the value", () => {
  const result = applyComposerEdit("", 0, `${ESC}[31mred${ESC}[0m`, {} as Key);

  assert.deepEqual(result, { value: "red", cursor: 3 });
});

test("a chunk carrying only control bytes is ignored", () => {
  const result = applyComposerEdit("AB", 1, `${ESC}[0m`, {} as Key);

  assert.deepEqual(result, { value: "AB", cursor: 1 });
});

// ── Audit 2026-07-26 P3: Forward Delete removes the grapheme ahead ──

test("forward delete removes the character after the cursor", () => {
  const edit = applyComposerEdit("abc", 1, "", { delete: true } as never);

  assert.equal(edit.value, "ac");
  assert.equal(edit.cursor, 1);
});

test("backspace still removes the character before the cursor", () => {
  const edit = applyComposerEdit("abc", 1, "", { backspace: true } as never);

  assert.equal(edit.value, "bc");
  assert.equal(edit.cursor, 0);
});

test("forward delete at the end of the line is a no-op", () => {
  const edit = applyComposerEdit("abc", 3, "", { delete: true } as never);

  assert.equal(edit.value, "abc");
  assert.equal(edit.cursor, 3);
});
