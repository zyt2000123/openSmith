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

/** Ink 6-compatible terminal size hook with stable non-TTY fallbacks. */
export function useWindowSize(): WindowSize {
  const { stdout } = useStdout();
  const [size, setSize] = useState(() => readWindowSize(stdout));

  useEffect(() => {
    const update = () => setSize(readWindowSize(stdout));
    update();
    stdout.on("resize", update);
    return () => {
      stdout.off("resize", update);
    };
  }, [stdout]);

  return size;
}
