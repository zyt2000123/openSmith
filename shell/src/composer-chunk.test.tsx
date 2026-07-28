import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import test from "node:test";

import { render } from "ink";
import { useState } from "react";

import { Composer } from "./composer.js";

/**
 * Audit 2026-07-26 P2: ink drains one stdin chunk in a synchronous loop, so
 * every event after the first used to see a stale `value` from the previous
 * render's closure and overwrite the edit before it. A chunk split on an escape
 * boundary ("abc<ESC>[Adef") kept only its last segment.
 *
 * Control bytes are built with fromCharCode to keep this file pure ASCII.
 */
const ESC = String.fromCharCode(27);

function fakeTtyStdin() {
  const stdin = new PassThrough();
  Object.assign(stdin, {
    isTTY: true,
    setRawMode() {
      return stdin;
    },
    ref() {},
    unref() {},
  });
  return stdin as unknown as NodeJS.ReadStream;
}

function fakeTtyStdout() {
  const out = new EventEmitter() as unknown as NodeJS.WriteStream;
  Object.assign(out, { isTTY: true, rows: 24, columns: 80, write: () => true });
  return out;
}

/** Mirrors how index.tsx drives the composer: controlled value in the store. */
function Harness({ onValue }: { onValue: (value: string) => void }) {
  const [value, setValue] = useState("");
  return (
    <Composer
      value={value}
      onChange={(next) => {
        setValue(next);
        onValue(next);
      }}
    />
  );
}

const tick = (ms = 60) => new Promise((resolve) => setTimeout(resolve, ms));

async function typeChunk(chunk: string): Promise<string> {
  const stdin = fakeTtyStdin();
  let latest = "";
  const instance = render(<Harness onValue={(value) => (latest = value)} />, {
    stdin,
    stdout: fakeTtyStdout(),
    exitOnCtrlC: false,
    patchConsole: false,
  });
  await tick(80);
  // One write: ink parses it into several events and dispatches them
  // synchronously, without an intervening re-render.
  (stdin as unknown as PassThrough).write(chunk);
  await tick(120);
  instance.unmount();
  instance.cleanup();
  return latest;
}

test("a chunk split by an escape sequence keeps every typed segment", async () => {
  // The arrow key between the two runs of text is what splits the chunk.
  const value = await typeChunk(`abc${ESC}[Adef`);

  assert.ok(value.includes("abc"), `lost the first segment: ${JSON.stringify(value)}`);
  assert.ok(value.includes("def"), `lost the last segment: ${JSON.stringify(value)}`);
});

test("a plain multi-character chunk is not truncated", async () => {
  const value = await typeChunk("hello");

  assert.equal(value, "hello");
});
