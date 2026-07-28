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

/** A CSI sequence, or the shorter two-byte ESC form; pasted terminal output carries these. */
// biome-ignore lint/suspicious/noControlCharactersInRegex: matching them is the point
const ANSI_SEQUENCE = /\u001b\[[0-9;?]*[\u0020-\u002f]*[\u0040-\u007e]|\u001b[\u0040-\u005a\u005c-\u005f]/g;
const LINE_BREAK = /\r\n?|\n/g;
// biome-ignore lint/suspicious/noControlCharactersInRegex: matching them is the point
const CONTROL_CHARACTER = /[\u0000-\u001f\u007f]/g;

/**
 * Reduce a raw input chunk to text this single-line composer can render safely:
 * terminal escapes are dropped, line breaks fold into spaces, and any remaining
 * control byte is removed so it can never reach Ink's renderer.
 */
export function sanitizeComposerInput(input: string): string {
  return input.replace(ANSI_SEQUENCE, "").replace(LINE_BREAK, " ").replace(CONTROL_CHARACTER, "");
}

export function applyComposerEdit(value: string, cursor: number, input: string, key: Key): ComposerEdit {
  const graphemes = splitComposerGraphemes(value);
  const position = Math.min(Math.max(0, cursor), graphemes.length);

  if (key.leftArrow) return { value, cursor: Math.max(0, position - 1) };
  if (key.rightArrow) return { value, cursor: Math.min(graphemes.length, position + 1) };
  if (key.home) return { value, cursor: 0 };
  if (key.end) return { value, cursor: graphemes.length };

  if (key.backspace) {
    if (position === 0) return { value, cursor: position };
    graphemes.splice(position - 1, 1);
    return { value: graphemes.join(""), cursor: position - 1 };
  }

  // ink 6.8 reported both DEL (0x7f) and Forward Delete (ESC [ 3 ~) as
  // `key.delete`, so the two could not be told apart and both deleted
  // backwards. ink 7 maps them separately, and the composer tracks the cursor,
  // so Forward Delete can finally remove the grapheme ahead of it.
  if (key.delete) {
    if (position >= graphemes.length) return { value, cursor: position };
    graphemes.splice(position, 1);
    return { value: graphemes.join(""), cursor: position };
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

  // Ink delivers a terminal paste - and an IME word commit - as one
  // multi-grapheme chunk, so the chunk itself carries no signal about intent.
  // Accept the text and strip what must never reach the renderer instead.
  const sanitized = sanitizeComposerInput(input);
  if (!sanitized) {
    return { value, cursor: position };
  }
  const inserted = splitComposerGraphemes(sanitized);
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
  // Ink's App.handleReadable drains one stdin chunk in a synchronous `while`
  // loop, calling the input handler once per parsed event, while React only
  // re-renders (and so only refreshes the handler's closure) afterwards. A
  // chunk split by an escape boundary — "abc<ESC>[Adef", an SSH-coalesced
  // burst, a paste containing ESC — therefore delivered every event after the
  // first a stale `value`/`cursor`, and each one overwrote the previous edit:
  // typing that lands as one chunk kept only its final segment. These refs are
  // the live values; the handler updates them before returning so the next
  // event in the same chunk sees the edit.
  const liveValue = useRef(value);
  const liveCursor = useRef(cursor);

  useEffect(() => {
    const length = splitComposerGraphemes(value).length;
    if (pendingLocalValue.current === value) {
      pendingLocalValue.current = null;
      setCursor((current) => {
        const next = Math.min(current, length);
        liveCursor.current = next;
        return next;
      });
    } else if (previousValue.current !== value) {
      // Changed from outside (history navigation, slash completion, a cleared
      // composer after submit): adopt it as the new live state.
      liveValue.current = value;
      liveCursor.current = length;
      setCursor(length);
    }
    previousValue.current = value;
  }, [value]);

  useInput(
    (input, key) => {
      const currentValue = liveValue.current;
      if (key.return) {
        onSubmit?.(currentValue);
        return;
      }

      const next = applyComposerEdit(currentValue, liveCursor.current, input, key);
      liveCursor.current = next.cursor;
      setCursor(next.cursor);
      if (next.value !== currentValue) {
        liveValue.current = next.value;
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
