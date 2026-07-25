# Agent-Smith

> Local-first, terminal-native AI agent workbench.

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-3178C6?style=for-the-badge&logo=typescript&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?style=for-the-badge&logo=sqlite&logoColor=white)
![React](https://img.shields.io/badge/React-61DAFB?style=for-the-badge&logo=react&logoColor=black)
![Anthropic](https://img.shields.io/badge/Claude-191919?style=for-the-badge&logo=anthropic&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-412991?style=for-the-badge&logo=openai&logoColor=white)

Smith is a single, always-on agent that runs locally. It keeps conversation context, accumulates memory across sessions, switches workflows via skills, and injects domain knowledge on demand. One agent, no orchestration overhead.

## What It Does

- **Interactive terminal** — rich Ink shell or plain CLI, your pick
- **Skill-based workflows** — debug, plan, refactor, review, or direct reply, chosen per task
- **Knowledge injection** — load domain context (frontend, backend, infra) without spawning new agents
- **Real tools** — file I/O, shell, Git, web search, MCP, all sandboxed with permission levels
- **Persistent memory** — sessions, agent memory, and project context survive restarts
- **Multi-provider LLM** — OpenAI-compatible, Anthropic, Gemini; routed by use case (interactive / gate / background)

## Quick Start

### Prerequisites

- Python 3.11+, [uv](https://docs.astral.sh/uv/), Node.js 18+

### Install

```bash
# Backend
cd server && uv sync

# Terminal shell — build, then link `smith` onto your PATH
cd ../shell && npm ci && npm run build && npm link
```

If an older release left a `smith` from `uv tool install`, remove it first — it
points at the deleted Python CLI and shadows the shell entry point:

```bash
uv tool uninstall agent-smith-server
```

`npm link` symlinks the global `smith` to this working copy rather than copying
it, so `smith` works from any directory and finds `server/` through its own
install path — but it breaks if the repository moves, and picks up source edits
only after `npm run build`. Verify with:

```bash
ls -l "$(which smith)"   # → .../lib/node_modules/smith-shell/bin/smith.js
```

### Configure

Set your LLM provider via environment variables:

```bash
export AGENTSMITH_LLM_PROVIDER=openai          # openai / anthropic / gemini
export AGENTSMITH_LLM_API_KEY="sk-..."
export AGENTSMITH_LLM_BASE_URL="https://api.openai.com/v1"
export AGENTSMITH_LLM_MODEL="your-model"
```

Or write `~/.agent-smith/config.yaml`:

```yaml
llm:
  provider: openai
  api_key: sk-...
  base_url: https://api.openai.com/v1
  model: your-model
```

### Run

```bash
# Run it from any project directory — it auto-starts the backend
smith
```

### Context files

`~/.agent-smith/SMITH.md` is your user-wide instruction file: it applies to every Smith run. Use a repository's `.smith/SMITH.md` only for rules that belong to that project. Both files are read when Smith builds a run, so edits apply to the next request without restarting the backend.

In the terminal, run `/reload` after editing context files to start a fresh session while keeping the prior session in history. The next task then starts with the current context.

## Architecture

```
shell (Ink / React)
      │ HTTP
      ▼
server (FastAPI + CLI)
      │
      ▼
engine (execution) ◄── agents (identity, skills, tools, safety)
      │
      ▼
common (config, SQLite, filesystem)
```

| Layer | What It Does |
|---|---|
| `common/` | Config, SQLite WAL, filesystem, logging — zero business logic |
| `engine/` | Task routing, skill chains, ReAct loop, LLM adapters, memory, tools, safety |
| `agents/` | Smith identity, pipelines, built-in skills, tool providers, safety rules |
| `server/` | FastAPI app, service orchestration, agent/session lifecycle, CLI |
| `shell/` | Ink/React terminal UI, auto-starts backend, SSE streaming |

Dependencies flow one way: `server → engine → common`. The engine never imports FastAPI.

## Development

```bash
cd engine && uv run --extra test pytest tests   # Engine tests
cd server && uv run --extra dev pytest tests     # Server tests
cd shell  && npm test && npm run check           # Shell tests + typecheck
```

## License

MIT
