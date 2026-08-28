# smith-desktop

Electron shell for Smith. The terminal shell in `shell/` is untouched and stays
the supported entry point — this reuses its logic rather than replacing it.

## How it reuses `shell/`

`shell/src` is imported verbatim; **no file under `shell/` was modified**. The
`shell-bridge` plugin in `electron.vite.config.ts` does two things at resolve
time:

1. maps shell's ESM-style `./foo.js` specifiers onto their `foo.ts` sources
2. swaps four modules that need Node or a TTY for renderer equivalents

| shell module | why it is swapped | renderer stand-in |
|---|---|---|
| `term.ts` | writes ANSI to stdout | no-op (DOM re-renders from state) |
| `dev-server.ts` | spawns uvicorn | IPC to main, which runs shell's original |
| `auth.ts` | reads `~/.agent-smith/auth_token` | IPC returns the finished header only |
| `history.ts` | writes a history file | `localStorage` |

`node:fs` and `node:path` are stubbed for `smith-ui-schema.ts`. Its fs calls are
a **security boundary** (path-traversal and decode-bomb checks), so the stub
throws and the caller's `catch` rejects the image. It fails closed on purpose.

Everything else — `api.ts`, `bridge.ts`, `store.ts`, `transcript-state.ts`,
`commands.ts`, `approval.ts` … ~5.4k lines — runs unmodified.

## Run

```bash
ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/ npm install   # mirror needed in CN
npm run dev          # renderer on :5173, CDP on :9333
npm run build
npm run typecheck
```

The main process starts the backend through shell's own `ensureLocalServer()`,
reusing an already-running server when one is healthy.

## Done / not done

Working: session tabs (local, so a tab exists before the backend creates the
session), backend boot, send + stream, markdown rendering, tool / skill /
thinking blocks, approval prompt, `/` command palette wired to shell's 21
commands, `@` skill mention, model switcher, session drawer, right sidebar with
six panels (sessions / skills / MCP / hooks / usage / runs), `⋯` menu.

Not yet: setup wizard, rich diff rendering, images, syntax highlighting in code
blocks, attachment picker behind the `+` button, `skill-actions` and
`hook-details` render as their parent list rather than a detail view.

## Layout notes

- `.lights` reserves a 74px strip so no tab can slide under the traffic lights.
  macOS greys the lights itself on blur — that is the system drawing them, not
  the app hiding them; `titleBarStyle: "hidden"` keeps the native buttons.
- Tabs are **local state**, not a projection of `state.sessions`. Smith creates
  a session lazily on the first message, so a tab bound to `currentSession`
  showed nothing in a fresh window.
- The sidebar opens itself whenever a command sets `store.panel`, and the usage
  and runs panels fetch on first open — before that, `/token` and `/runs` wrote
  state that nothing rendered and looked broken.

## Markdown safety

`Markdown.tsx` walks marked's **token tree** with React components rather than
setting `innerHTML`. Assistant text is untrusted, so never building an HTML
string means there is no injection surface to sanitise — and no DOMPurify
dependency. Raw HTML in the source renders as text; only `http(s)` links are
clickable.

## Known constraints

- `sandbox: true` requires a **CommonJS** preload — hence the `.cjs` output.
- Electron's cwd is `desktop/`, so `SMITH_REPO_ROOT` is set in `main/index.ts`
  before shell's `resolveRepoRoot()` reads it, and `smith:working-dir` returns
  the checkout rather than `process.cwd()`.
