import assert from "node:assert/strict";
import test from "node:test";

import { sanitizeTerminalText, sanitizeUnknownText } from "./sanitize.js";

/**
 * Control bytes are built with fromCharCode so this file stays pure ASCII: a
 * literal control character in source is invisible in review, which is the
 * failure mode being defended against.
 */
const ESC = String.fromCharCode(27);
const BEL = String.fromCharCode(7);
const CR = String.fromCharCode(13);
const CSI_8BIT = String.fromCharCode(0x9b);
const OSC_8BIT = String.fromCharCode(0x9d);

test("strips an OSC 52 clipboard write", () => {
  // The headline attack: replace what the user just copied, so their next
  // paste runs something else.
  assert.equal(sanitizeTerminalText(`${ESC}]52;c;bWFsaWNpb3Vz${BEL}ok`), "ok");
});

test("strips OSC sequences that forge terminal state", () => {
  assert.equal(sanitizeTerminalText(`${ESC}]7;file://host/etc${BEL}text`), "text");
  assert.equal(sanitizeTerminalText(`${ESC}]0;fake title${BEL}body`), "body");
  assert.equal(sanitizeTerminalText(`${ESC}]8;;http://evil${BEL}click here`), "click here");
});

test("strips an unterminated OSC sequence to end of input", () => {
  assert.equal(sanitizeTerminalText(`a${ESC}]52;c;never-terminated`), "a");
});

test("strips an OSC sequence terminated by ST rather than BEL", () => {
  assert.equal(sanitizeTerminalText(`${ESC}]0;t${ESC}\\after`), "after");
});

test("strips CSI sequences and two-character escapes", () => {
  assert.equal(sanitizeTerminalText(`${ESC}[2J${ESC}[1;1Hhome`), "home");
  assert.equal(sanitizeTerminalText(`${ESC}cx`), "x");
});

test("strips carriage returns, which rewrite the printed line", () => {
  assert.equal(sanitizeTerminalText(`aaa${CR}bbb`), "aaabbb");
});

test("strips bell and 8-bit sequence introducers", () => {
  assert.equal(sanitizeTerminalText(`ding${BEL}`), "ding");
  // The introducer goes; its payload is left as inert visible text.
  assert.equal(sanitizeTerminalText(`x${CSI_8BIT}2Jy`), "x2Jy");
  assert.equal(sanitizeTerminalText(`x${OSC_8BIT}52;c;zzz`), "x52;c;zzz");
});

test("keeps text that rendering needs", () => {
  assert.equal(sanitizeTerminalText("a\nb\tc"), "a\nb\tc");
  assert.equal(sanitizeTerminalText("中文 🌙 ok"), "中文 🌙 ok");
  assert.equal(sanitizeTerminalText(""), "");
});

test("leaves no escape byte behind for any payload", () => {
  const payloads = [
    `${ESC}]52;c;x${BEL}`,
    `${ESC}]0;t${ESC}\\`,
    `${ESC}[38;5;9m`,
    `${ESC}]8;;http://x${BEL}y`,
    `${ESC}${ESC}[2J`,
  ];

  for (const payload of payloads) {
    assert.ok(!sanitizeTerminalText(payload).includes(ESC), `escape byte survived: ${JSON.stringify(payload)}`);
  }
});

test("bidi overrides are stripped so a rendered command cannot lie", () => {
  // Built from code points, not literals: an RLO pasted into source is invisible
  // in review, which is the whole point of stripping it.
  const RLO = String.fromCharCode(0x202e);
  const LRI = String.fromCharCode(0x2066);
  const PDI = String.fromCharCode(0x2069);

  assert.equal(sanitizeTerminalText(`rm -rf /${RLO}tmp${PDI}`), "rm -rf /tmp");
  assert.equal(sanitizeTerminalText(`${LRI}safe${PDI}`), "safe");
  // Strong RTL characters are content, not formatting, and must survive.
  assert.equal(sanitizeTerminalText("مرحبا"), "مرحبا");
});

test("sanitizeUnknownText tolerates non-strings", () => {
  assert.equal(sanitizeUnknownText(`x${BEL}`), "x");
  assert.equal(sanitizeUnknownText(undefined), "");
  assert.equal(sanitizeUnknownText(null), "");
  assert.equal(sanitizeUnknownText(42), "");
});
