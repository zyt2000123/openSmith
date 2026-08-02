# Agent-Smith

> Local-first, terminal-native AI agent workbench.

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-3178C6?style=for-the-badge&logo=typescript&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?style=for-the-badge&logo=sqlite&logoColor=white)
![React](https://img.shields.io/badge/React-61DAFB?style=for-the-badge&logo=react&logoColor=black)
![Anthropic](https://img.shields.io/badge/Claude-191919?style=for-the-badge&logo=anthropic&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-412991?style=for-the-badge&logo=openai&logoColor=white)

**Smith is a single, always-on agent** that runs locally in your terminal. It keeps conversation context, accumulates memory across sessions, and switches workflows via skills. One agent, one conversation, no orchestration overhead.

## What It Does

- **Single Agent Architecture** — Smith is your single resident assistant, no sub-agents or multi-agent routing
- **Interactive Terminal** — rich Ink shell with SSE streaming, auto-starts the backend
- **Skill-based Workflows** — coding pipelines (understanding → planning → architecture → implementation → validation), grilling sessions, research, or direct ReAct loop
- **Task Routing** — keyword-based routing to pipelines or direct execution, configured per identity
- **Real Tools** — file I/O, shell, Git, web search, MCP servers, all sandboxed with permission levels
- **Persistent Memory** — sessions, agent state, and project context survive restarts
- **Multi-provider LLM** — OpenAI-compatible, Anthropic, Gemini; routed by use case (interactive / gate / background)

## Quick Start

### Prerequisites

- Python 3.11+, [uv](https://docs.astral.sh/uv/), Node.js 22+

### Install

```bash
# Backend
cd server && uv sync

# Terminal shell — build, then link `smith` onto your PATH
cd ../shell && npm ci && npm run build && npm link
```

If an older release left a `smith` from `uv tool install`, remove it first:

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

### Context Files

Agent-Smith reads instructions from two locations:

- **User-wide**: `~/.agent-smith/config.yaml` — applies to every Smith run
- **Project-specific**: `.smith/SMITH.md` in the repository root — rules that belong to this project only

Create a project instruction file with:

```bash
smith
# Then in the shell:
/init
```

After editing context files, use `/reload` to start a fresh session:

```bash
/reload
```

The next task will start with the updated context without restarting the backend.

## Architecture

```
shell (Ink / React)
      │ HTTP + SSE
      ▼
server (FastAPI)
      │
      ▼
engine (execution) ◄── agents (identity, skills, tools, safety)
      │
      ▼
common (paths, YAML, SQLite)
```

| Layer | What It Does |
|---|---|
| `common/` | Paths, YAML configuration, SQLite connection, filesystem resources — zero business logic |
| `engine/` | Task routing, skill chains, ReAct loop, LLM adapters, memory, tools, safety |
| `agents/` | Smith identity seed, pipelines, built-in skills, tool providers, safety rules |
| `server/` | FastAPI app, service orchestration, agent/session lifecycle, HTTP endpoints |
| `shell/` | Ink/React terminal UI, auto-starts backend, SSE streaming, command handling |

**Dependency flow**: `server → engine → common` (one-way, enforced by tests).  
The engine never imports FastAPI or server concepts.

## Task Routing

Smith routes tasks based on keyword matching in `agents/identities/smith.yaml`:

```yaml
routes:
  - keywords: ["bug", "fix", "defect"]
    pipeline: coding
    priority: 100
  - keywords: ["refactor", "clean up"]
    pipeline: coding
    priority: 90
  - keywords: ["feature", "implement"]
    pipeline: coding
    priority: 80
```

When a pipeline is matched, Smith runs through its stages (e.g., understanding → planning → architecture → implementation → validation). Each stage has a gate that validates output before proceeding. If a gate fails, Smith backtracks to an earlier node.

When no pipeline matches, Smith uses the direct ReAct loop.

## Coding Pipeline

The `coding` pipeline (`agents/pipelines/coding.yaml`) has 5 stages:

1. **Understanding** — analyze requirements, gather context
2. **Planning** — break down into tasks, identify dependencies
3. **Architecture** — design system structure (conditional: only runs when `needs_architecture` holds)
4. **Implementation** — write code, run tests
5. **Validation** — verify correctness, run final checks

Each stage has a corresponding gate (understanding, planning, design, contract_alignment, validation_llm) that validates the output before proceeding to the next stage.

## Built-in Skills

Skills are discovered from `agents/skills/` directories containing a `SKILL.md` file:

- `coding-understanding` — analyze requirements and codebase
- `coding-planning` — break down tasks and dependencies
- `coding-architecture` — design system structure
- `coding-implementation` — write code with TDD
- `coding-validation` — verify correctness and test coverage
- `grill-me` / `grilling` — clarify requirements through questioning
- `research` — web search and information gathering
- `diagnosing-bugs` — systematic bug investigation
- `tdd-workflow` — test-driven development loop
- `verification-loop` — iterative verification
- `code-review` — code quality and security review
- `teach` — learning and knowledge transfer
- `writing-great-skills` — skill authoring guide

Skills are mirrored to `~/.agent-smith/builtin/skills/` on first run. User-installed skills go in `~/.agent-smith/agent/skills/`.

## Documentation

The documentation landing page separates source-backed implementation contracts
from historical design and research notes: [docs/README.md](docs/README.md).

Key documents:
- [CLAUDE.md](CLAUDE.md) — working brief for AI agents operating in this repository
- [docs/README.md](docs/README.md) — documentation index
- [server/docs/fixes/common-module-improvements.md](server/docs/fixes/common-module-improvements.md) — recent infrastructure improvements

## Development

```bash
# Install dependencies
cd engine && uv sync
cd server && uv sync
cd shell  && npm ci

# Run tests
cd engine && uv run --with pytest --with pytest-asyncio pytest
cd server && uv run --with pytest --with pytest-asyncio pytest
cd shell  && npm test && npm run check

# Current baseline: engine 717 passed, server 155 passed (5 skipped)
```

### Testing the Shell

```bash
cd shell
npm test                # Unit tests
npm run check           # TypeScript type checking
npm run build           # Build for production
```

## License

MIT
