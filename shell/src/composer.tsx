import { type Key, Text, useInput } from "ink";
import { useEffect, useRef, useState } from "react";

export type ComposerEdit = {
  value: string;
  /** Cursor position measured in grapheme clusters. */
  cursor: number;
};

export type ComposerProps = {
  value: string;
  placeholder?: string;
  focus?: boolean;
  mask?: string;
  showCursor?: boolean;
  onChange: (value: string) => void;
  onSubmit?: (value: string) => void;
};

const graphemeSegmenter =
  typeof Intl.Segmenter === "function" ? new Intl.Segmenter(undefined, { granularity: "grapheme" }) : null;

export function splitComposerGraphemes(value: string): string[] {
  if (graphemeSegmenter) {
    return Array.from(graphemeSegmenter.segment(value), ({ segment }) => segment);
  }
  return Array.from(value);
}

export function applyComposerEdit(value: string, cursor: number, input: string, key: Key): ComposerEdit {
  const graphemes = splitComposerGraphemes(value);
  const position = Math.min(Math.max(0, cursor), graphemes.length);

  if (key.leftArrow) return { value, cursor: Math.max(0, position - 1) };
  if (key.rightArrow) return { value, cursor: Math.min(graphemes.length, position + 1) };
  if (key.home) return { value, cursor: 0 };
  if (key.end) return { value, cursor: graphemes.length };

  if (key.backspace || key.delete) {
    if (position === 0) return { value, cursor: position };
    graphemes.splice(position - 1, 1);
    return { value: graphemes.join(""), cursor: position - 1 };
  }

  if (
    !input ||
    key.return ||
    key.escape ||
    key.tab ||
    key.upArrow ||
    key.downArrow ||
    key.pageUp ||
    key.pageDown ||
    key.ctrl ||
    key.meta ||
    key.super ||
    key.hyper
  ) {
    return { value, cursor: position };
  }

  const inserted = splitComposerGraphemes(input);
  // Ink presents a terminal paste as one multi-grapheme input chunk.  Keep the
  // composer to preserve ordinary key entry, but deliberately reject that
  // batch input now that paste support has been removed.
  const hasControlCharacter = Array.from(input).some((character) => {
    const codePoint = character.codePointAt(0);
    return codePoint !== undefined && (codePoint <= 0x1f || codePoint === 0x7f);
  });
  if (inserted.length !== 1 || hasControlCharacter) {
    return { value, cursor: position };
  }
  graphemes.splice(position, 0, ...inserted);
  return { value: graphemes.join(""), cursor: position + inserted.length };
}

export function Composer({
  value,
  placeholder = "",
  focus = true,
  mask,
  showCursor = true,
  onChange,
  onSubmit,
}: ComposerProps) {
  const [cursor, setCursor] = useState(() => splitComposerGraphemes(value).length);
  const previousValue = useRef(value);
  const pendingLocalValue = useRef<string | null>(null);

  useEffect(() => {
    const length = splitComposerGraphemes(value).length;
    if (pendingLocalValue.current === value) {
      pendingLocalValue.current = null;
      setCursor((current) => Math.min(current, length));
    } else if (previousValue.current !== value) {
      setCursor(length);
    }
    previousValue.current = value;
  }, [value]);

  useInput(
    (input, key) => {
      if (key.return) {
        onSubmit?.(value);
        return;
      }

      const next = applyComposerEdit(value, cursor, input, key);
      setCursor(next.cursor);
      if (next.value !== value) {
        pendingLocalValue.current = next.value;
        onChange(next.value);
      }
    },
    { isActive: focus },
  );

  const rawGraphemes = splitComposerGraphemes(value);
  const visibleGraphemes = mask ? rawGraphemes.map(() => mask) : rawGraphemes;
  if (visibleGraphemes.length === 0) {
    const placeholderGraphemes = splitComposerGraphemes(placeholder);
    const first = placeholderGraphemes[0] ?? " ";
    const rest = placeholderGraphemes.slice(1).join("");
    return showCursor && focus ? (
      <Text>
        <Text inverse>{first}</Text>
        <Text dimColor>{rest}</Text>
      </Text>
    ) : (
      <Text dimColor>{placeholder}</Text>
    );
  }

  if (!showCursor || !focus) return <Text>{visibleGraphemes.join("")}</Text>;
  const position = Math.min(cursor, visibleGraphemes.length);
  return (
    <Text>
      {visibleGraphemes.slice(0, position).join("")}
      <Text inverse>{visibleGraphemes[position] ?? " "}</Text>
      {visibleGraphemes.slice(position + 1).join("")}
    </Text>
  );
}
