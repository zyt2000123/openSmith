# CLI Markdown table rendering: Codex CLI and CodeBuddy Code

> Scope: primary implementation sources only. This is an implementation note for
> the Shell transcript, not a claim that Agent-Smith already behaves this way.
> The question is specifically whether a terminal can preserve **all table
> content** at a narrow width without silently truncating cells.

## Conclusion

Both examined CLIs preserve table content, but neither promises to preserve a
single horizontal grid at every terminal width:

| CLI | Rendering stack | Normal-width table | Narrow-width result | Silent cell truncation / horizontal scrolling |
| --- | --- | --- | --- | --- |
| Codex CLI | Rust `ratatui` + `pulldown-cmark` + a custom table renderer | Styled, aligned, row-separated grid | Shrinks columns and wraps cells; when the grid no longer scans well, transposes each body row into labelled key/value records | No truncation or horizontal-scroll implementation in this path |
| CodeBuddy Code 2.125.0 | `marked` lexer + custom ANSI formatter + one Ink `Text` element | Unicode box/grid table | Wraps cells (hard-breaks if necessary); once a row exceeds four rendered lines, renders `Header: full content` fields vertically | No ellipsis/truncation or horizontal-scroll implementation in the formatter |

So the transferable design is **content-preserving responsive table rendering**:
keep a grid while it is legible, wrap first, then make the table vertical while
preserving each field in full. A horizontal grid containing every character is
not physically possible in a narrow terminal without either wrapping, clipping,
or horizontal scrolling. Neither reference CLI implements transcript-local
horizontal scrolling for Markdown tables.

## Codex CLI: purpose-built, width-aware Rust renderer

The locally installed `codex-cli 0.145.0` is a native binary reached through
the `@openai/codex` npm launcher, so its Rust source is not shipped in the local
npm package. The upstream [Codex source snapshot at
`cefcffd`](https://github.com/openai/codex/tree/cefcffd692a3d070e9341ffa756a04e56d086c19)
is the directly inspectable implementation baseline below; it can differ from
the installed release and must be rechecked when upgrading.

- The TUI depends on [`ratatui` and `pulldown-cmark`](https://github.com/openai/codex/blob/cefcffd692a3d070e9341ffa756a04e56d086c19/codex-rs/tui/Cargo.toml). It is not using a generic React/Ink Markdown-table component.
- [`markdown.rs`](https://github.com/openai/codex/blob/cefcffd692a3d070e9341ffa756a04e56d086c19/codex-rs/tui/src/markdown.rs) passes the known viewport width into the renderer. [`markdown_render.rs`](https://github.com/openai/codex/blob/cefcffd692a3d070e9341ffa756a04e56d086c19/codex-rs/tui/src/markdown_render.rs) enables `pulldown-cmark` tables, gathers table events into structured cells, and calculates display widths using `unicode-width`.
- Its documented pipeline is: normalize rows, allocate content-aware column widths, render an aligned grid while it remains scannable, otherwise render body rows as key/value records. `render_table_lines()` reserves the terminal prefix, inter-column gaps, and cell padding before allocation; `render_table_row()` calls `wrap_cell()` for every cell. This is wrapping, not clipping.
- The key/value fallback is in [`markdown_render/table_key_value.rs`](https://github.com/openai/codex/blob/cefcffd692a3d070e9341ffa756a04e56d086c19/codex-rs/tui/src/markdown_render/table_key_value.rs). It word-wraps labels and values, supports an aligned `label  value` form when possible, and stacks them when even that does not fit.
- The renderer's own tests cover a wrapped grid, a narrow path column that becomes key/value records, systemic compact-cell fragmentation, and a ten-column table whose 20-column viewport becomes records rather than an unusable grid. See [`markdown_render_tests.rs`](https://github.com/openai/codex/blob/cefcffd692a3d070e9341ffa756a04e56d086c19/codex-rs/tui/src/markdown_render_tests.rs).

One deliberate exception matters for any “never hide text” requirement:
Codex's general `adaptive_wrap_line()` can keep a URL token intact to preserve
clickability, which can allow that *URL line* to exceed the viewport. Table
cells themselves call the ordinary `word_wrap_line()` path, whose default
options have `break_words: true`; see [`wrapping.rs`](https://github.com/openai/codex/blob/cefcffd692a3d070e9341ffa756a04e56d086c19/codex-rs/tui/src/wrapping.rs).
Thus the table path favours full visible content over retaining an unbroken URL.

## CodeBuddy Code: bundled custom formatter above Ink

This machine has the official package `@tencent-ai/codebuddy-code` **2.125.0**:
`/opt/homebrew/bin/codebuddy` resolves to its npm `bin/codebuddy`, and
`codebuddy --version` returns `2.125.0`. Tencent's [official install
documentation](https://www.codebuddy.cn/docs/cli/installation) names this
package; the package identifies its upstream homepage as
[`codebuddy/codebuddy-code`](https://cnb.cool/codebuddy/codebuddy-code).

The installed production artifact is the primary source for this exact version:

- 本地安装包中的 `dist/codebuddy.js`（原始机器路径不纳入仓库）、webpack module `65077`（起始约 byte `11,910,185`）定义了 `Markdown`、`applyMarkdown`、`formatTable`、`renderHorizontalTable` 和 `renderVerticalTable`。
- Its package metadata records `ink` `6.3.1`, `marked` `^16.1.1`, and React `~19.1.2`. `Markdown` lexes with `marked`, formats ANSI strings, then returns a single Ink `Text`; the Markdown table branch calls the custom `formatTable`, not the separately bundled `cli-table3`.
- `formatTable` reads `terminalWidth` from Ink's static context (default `80`), computes minimum/preferred display widths through bundled `string-width` (including wide characters), and wraps cells. If the minimum grid cannot fit, it sets `hardBreak` and splits text further rather than dropping it.
- It sets a maximum of four wrapped lines per horizontal row. Once a row would exceed that, `renderVerticalTable` emits each field as `Header: full content`. The bundle's Markdown-table formatter contains no ellipsis insertion, cell truncation, or horizontal-scrolling branch.

The published tarball for this exact artifact is
[`codebuddy-code-2.125.0.tgz`](https://registry.npmjs.org/@tencent-ai/codebuddy-code/-/codebuddy-code-2.125.0.tgz)
(integrity `sha512-GQPlSdhkL+uIQ+dfMjPbvLXYAPferSVy3O43EE1L/q1I42biSsMFop2ptZpkMJGgE4NbOI2QD9jGOJJdhCA6og==`).
It contains no source map, so original TypeScript file paths and line numbers
cannot be honestly attributed to this package version. The public repository's
latest visible commit must not be treated as the source commit for 2.125.0.

## Implication for Agent-Smith Shell

The earlier “replace a narrow table with a card” direction has the right
content-safety property, but the desired presentation should be named and
tested more precisely:

1. Make the Markdown renderer receive the actual Transcript width.
2. Keep its existing table syntax/grid when all columns have useful widths.
3. Word-wrap every cell using terminal display width (CJK and emoji included),
   never an ellipsis/truncate option.
4. When the grid reaches a legibility threshold, use a semantic **vertical
   table** (`header: complete value` for every cell) with row separators—not a
   generic UI card that changes the document's meaning.
5. Test both exact content preservation and width bounds for prose, CJK, long
   unspaced IDs/paths, URLs, many columns, and streamed partial tables.

This preserves every value while acknowledging the terminal's real width
constraint. It is also a closer behavioural match to both CLI references than
either unbounded one-line grids or default library truncation.

## Agent-Smith decision and implemented boundary

Agent-Smith deliberately takes a narrower product decision than either
reference: **do not transpose a Markdown table into cards or key/value records**.
The Shell keeps a Unicode grid, reserves transcript indentation before it
allocates columns, and hard-wraps every cell by terminal display width. This is
the requested representation even when a row becomes tall.

The implementation lives in `shell/src/markdown-table.tsx` and uses the
`marked` AST instead of splitting pipe characters. `shell/src/text-layout.ts`
is the shared content-preserving width primitive: normal prose may keep a URL
or identifier intact, while grids and structured diffs opt into hard breaks.
The active transcript is split by `shell/src/streaming-markdown.ts` so an
unfinished table remains in the dynamic region until its boundary is complete;
this does not change the SSE, Bridge, Store, or Engine lifecycle.

At an extreme width where even one terminal cell per column plus the grid
borders cannot fit, `layoutMarkdownTable()` sets `overflowed: true` and keeps
the smallest valid grid rather than silently deleting content or changing the
document into another representation. That is a physical display constraint,
not a hidden fallback. The test suite covers parser correctness (including
escaped pipes), CJK/display-width wrapping, full long-token retention,
streamed table holdback, and actual 40-column Ink rendering.

The related reusable primitives are `DisplayText` (non-truncating text),
`MarkdownTableBlock` (GFM grid), and `DiffBlock` (unified-diff gutters and
word-level change emphasis). `PanelContainer`, `TabbedPanel`, and
`MultiSelectList` now provide only presentational composition; existing
`input.ts` plus `list-navigation.ts` remain the single keyboard, focus, and
visible-window system. The adoption boundary and human development review
checklist are recorded in
[`2026-07-22-shell-component-adoption.md`](2026-07-22-shell-component-adoption.md).
