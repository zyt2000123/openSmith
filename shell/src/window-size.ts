import { useStdout } from "ink";
import { useEffect, useState } from "react";

export type WindowSize = {
  columns: number;
  rows: number;
};

function readWindowSize(stdout: NodeJS.WriteStream): WindowSize {
  return {
    columns: Math.max(1, stdout.columns || 80),
    rows: Math.max(1, stdout.rows || 24),
  };
}

/**
 * One resize listener per stdout, shared by every consumer of the hook.
 *
 * `TranscriptEntryView` calls the hook once per transcript entry, and the
 * transcript holds up to 200 of them. A listener per instance passed Node's
 * default limit of 10 as soon as ~11 entries were mounted — and Ink mounts them
 * all at once whenever `<Static>` remounts — which printed
 * `MaxListenersExceededWarning` to stderr, straight into the render area.
 */
type ResizeSubscription = {
  listeners: Set<(size: WindowSize) => void>;
  detach: () => void;
};

const subscriptions = new WeakMap<NodeJS.WriteStream, ResizeSubscription>();

function subscribe(stdout: NodeJS.WriteStream, listener: (size: WindowSize) => void): () => void {
  let subscription = subscriptions.get(stdout);
  if (!subscription) {
    const created: ResizeSubscription = { listeners: new Set(), detach: () => {} };
    const onResize = () => {
      const size = readWindowSize(stdout);
      for (const notify of created.listeners) notify(size);
    };
    stdout.on("resize", onResize);
    created.detach = () => stdout.off("resize", onResize);
    subscriptions.set(stdout, created);
    subscription = created;
  }

  subscription.listeners.add(listener);
  return () => {
    const current = subscriptions.get(stdout);
    if (!current) return;
    current.listeners.delete(listener);
    if (current.listeners.size === 0) {
      current.detach();
      subscriptions.delete(stdout);
    }
  };
}

/** Live subscriber count for one stdout; lets a test assert there is no leak. */
export function resizeListenerCount(stdout: NodeJS.WriteStream): number {
  return subscriptions.get(stdout)?.listeners.size ?? 0;
}

/** Ink 6-compatible terminal size hook with stable non-TTY fallbacks. */
export function useWindowSize(): WindowSize {
  const { stdout } = useStdout();
  const [size, setSize] = useState(() => readWindowSize(stdout));

  useEffect(() => {
    // Re-read on subscribe: the terminal may have resized between the initial
    // render and this effect running.
    setSize(readWindowSize(stdout));
    return subscribe(stdout, setSize);
  }, [stdout]);

  return size;
}
