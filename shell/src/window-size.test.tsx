import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";

import { Box, render, Text } from "ink";

import { resizeListenerCount, useWindowSize } from "./window-size.js";

/**
 * Audit 2026-07-26 P2: TranscriptEntryView calls useWindowSize once per
 * transcript entry (up to 200), and Ink mounts them all at once when <Static>
 * remounts. One stdout listener per instance passed Node's default limit of 10
 * and printed MaxListenersExceededWarning into the render area.
 */
function fakeTty() {
  const out = new EventEmitter() as unknown as NodeJS.WriteStream & { writes: string[] };
  Object.assign(out, {
    isTTY: true,
    rows: 24,
    columns: 80,
    writes: [] as string[],
    write(chunk: string) {
      (out as unknown as { writes: string[] }).writes.push(String(chunk));
      return true;
    },
  });
  return out;
}

function fakeStdin() {
  const stdin = new EventEmitter();
  Object.assign(stdin, {
    isTTY: true,
    setRawMode() {},
    ref() {},
    unref() {},
    read: () => null,
    setEncoding() {},
    resume() {},
    pause() {},
  });
  return stdin as unknown as NodeJS.ReadStream;
}

function Consumer() {
  const { columns } = useWindowSize();
  return <Text>{columns}</Text>;
}

function Many({ count }: { count: number }) {
  return (
    <Box flexDirection="column">
      {Array.from({ length: count }, (_unused, index) => `c${index}`).map((key) => (
        <Consumer key={key} />
      ))}
    </Box>
  );
}

const tick = (ms = 60) => new Promise((resolve) => setTimeout(resolve, ms));

/** Mount `count` consumers, return the stdout listener total while mounted. */
async function listenersWhileMounted(
  count: number,
): Promise<{ total: number; subscribers: number; afterUnmount: number }> {
  const stdout = fakeTty();
  const instance = render(<Many count={count} />, {
    stdout,
    stdin: fakeStdin(),
    exitOnCtrlC: false,
    patchConsole: false,
  });
  await tick();
  const total = stdout.listenerCount("resize");
  const subscribers = resizeListenerCount(stdout);

  instance.unmount();
  instance.cleanup();
  await tick();

  return { total, subscribers, afterUnmount: resizeListenerCount(stdout) };
}

test("stdout resize listeners do not grow with the number of consumers", async () => {
  // Ink registers its own resize listener for repaints, so the absolute count is
  // not 1. What matters is that it does not scale with transcript length.
  const few = await listenersWhileMounted(5);
  const many = await listenersWhileMounted(40);

  assert.equal(few.subscribers, 5);
  assert.equal(many.subscribers, 40);
  assert.equal(many.total, few.total, `listener total grew from ${few.total} to ${many.total}`);
  assert.ok(many.total <= 2, `expected ink's listener plus one shared listener, got ${many.total}`);
});

test("subscribers detach when their consumers unmount", async () => {
  const { afterUnmount } = await listenersWhileMounted(12);

  assert.equal(afterUnmount, 0);
});

test("no MaxListenersExceededWarning reaches the render area", async () => {
  const warnings: string[] = [];
  const onWarning = (warning: Error) => warnings.push(warning.name);
  process.on("warning", onWarning);

  const stdout = fakeTty();
  const instance = render(<Many count={40} />, {
    stdout,
    stdin: fakeStdin(),
    exitOnCtrlC: false,
    patchConsole: false,
  });
  await tick();
  instance.unmount();
  instance.cleanup();
  await tick();
  process.off("warning", onWarning);

  assert.deepEqual(
    warnings.filter((name) => name === "MaxListenersExceededWarning"),
    [],
  );
});
