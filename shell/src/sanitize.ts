/**
 * Neutralise terminal control sequences in untrusted text.
 *
 * Everything the server sends is untrusted for this purpose: model output, tool
 * results (arbitrary command output, file contents, fetched pages), and skill or
 * hook text. Ink passes strings straight through to stdout, and while it happens
 * to absorb CSI sequences via its own line handling, OSC sequences arrive at the
 * terminal intact. That is not cosmetic — OSC 52 writes the system clipboard
 * (default-allowed in kitty/wezterm/alacritty), OSC 7 forges the reported cwd,
 * OSC 0 forges the window title, and OSC 8 hides a phishing target behind
 * innocuous link text.
 *
 * Sanitising happens at the decode boundary rather than inside each renderer, so
 * a new render path cannot forget to do it.
 *
 * The patterns are built with `new RegExp` from escaped strings so this file
 * holds no literal control characters — a control byte pasted into source is
 * invisible in review, which is exactly the failure being defended against.
 */

/**
 * A 7-bit escape sequence: OSC (terminated by BEL or ST, or left unterminated at
 * end of input), CSI, or a two-character sequence.
 *
 * OSC is matched first because its payload may contain characters that would
 * otherwise look like the start of a CSI sequence.
 */
const ESCAPE_SEQUENCE = new RegExp(
  [
    // OSC: ESC ] ... (BEL | ESC \), payload stops at either terminator
    "\\u001b\\][^\\u0007\\u001b]*(?:\\u0007|\\u001b\\\\)?",
    // CSI: ESC [ params intermediates final
    "\\u001b\\[[0-9;?]*[\\u0020-\\u002f]*[\\u0040-\\u007e]",
    // Everything else: ESC, optional intermediates, one final byte. The final
    // byte spans 0x30-0x7e because the dangerous ones are spread across all
    // three classes — Fp (ESC 7/8, cursor save/restore), Fe (0x40-0x5f), and
    // Fs (ESC c, full reset, at 0x63). Intermediates cover ESC ( B and kin.
    // Reached only after the CSI and OSC branches decline, so it reads a
    // malformed one as a bare escape rather than leaving its payload behind.
    "\\u001b[\\u0020-\\u002f]*[\\u0030-\\u007e]",
  ].join("|"),
  "g",
);

/**
 * Control characters with no place in rendered text.
 *
 * Tab (09) and newline (0a) survive — markdown needs them. Carriage return is
 * not spared: on a terminal it rewrites the current line, which is how a tool
 * result can overwrite what the shell just printed. C1 (80-9f) covers the 8-bit
 * forms of CSI and OSC; stripping the introducer leaves the payload behind as
 * inert visible text.
 */
// biome-ignore lint/complexity/useRegexLiterals: a literal would put control characters in the source, where they are invisible in review — the exact failure this module defends against. See the file header.
const UNSAFE_CONTROL = new RegExp("[\\u0000-\\u0008\\u000b-\\u001f\\u007f-\\u009f]", "g");

/**
 * Strip escape sequences and unsafe control characters, keeping tabs/newlines.
 *
 * Order matters: escape sequences go first, because stripping the lone ESC byte
 * would leave its payload (`]52;c;<base64>`) behind as visible text.
 */
export function sanitizeTerminalText(text: string): string {
  if (!text) return text;
  return text.replace(ESCAPE_SEQUENCE, "").replace(UNSAFE_CONTROL, "");
}

/** Sanitise a value that should be a string, tolerating anything else. */
export function sanitizeUnknownText(value: unknown): string {
  return typeof value === "string" ? sanitizeTerminalText(value) : "";
}
