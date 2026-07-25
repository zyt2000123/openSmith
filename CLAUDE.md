# CLAUDE.md

This file is the working brief for Claude or any coding agent operating in this repository.

## 1. What This Project Is

Agent-Smith is a local-first personal assistant Agent workbench that runs in the terminal.

- Smith is the single, always-on Agent
- Smith uses the skill system to switch workflows per task type (debug, planning, review, etc.)
- Smith uses knowledge injection to gain domain expertise when needed (frontend patterns, backend patterns, etc.)
- No sub-agents, no multi-agent routing — one Agent, one conversation, accumulating memory over time

One-line:

> Agent-Smith is a local-first Agent workbench. Smith is your single resident assistant — it keeps context, accumulates memory, switches workflows via skills, and injects domain knowledge when a task demands it.

## 2. Current Priority

The current priority is the terminal workbench experience:

1. Smith CLI (chat, sessions, agent management) works end-to-end
2. Ink shell (`shell/`) provides the rich terminal UI
3. Skill-based workflow switching works for different task types
4. Memory accumulation across sessions

## 3. Agent Architecture

```
User ←→ Smith (single Agent, always-on)
           │
           ├── skill system (workflow per task type)
           │    ├── sde-debug → planning → testing → validation → review
           │    ├── planning → architecture → testing → validation → review
           │    └── direct reply (simple questions)
           │
           └── knowledge injection (domain expertise on demand)
                ├── frontend knowledge (React, CSS, build tools, browser)
                └── backend knowledge (API, DB, infra, performance)
```

Smith decides everything. Skills switch the workflow. Knowledge injection provides domain context. No agent routing needed.

## 4. Product Language

Use: "Smith", "Agent", "skill", "knowledge", "session", "memory", "tool", "template"

Avoid: "sub-agent", "employee", "digital employee", "hire"

## 5. Architecture Boundaries

Four layers, one-way dependencies:

```
server/ → engine/ → common/
          ↑
        agents/
```

Plus `shell/` as the terminal frontend (Ink/React, calls server via HTTP).

| Layer | Directory | Responsibility |
|---|---|---|
| Infrastructure | `common/` | Config, SQLite, filesystem, logging. Zero business logic. |
| Execution | `engine/` | Agent framework: LLM, DAG+ReAct, memory, skills, tools, safety. Zero platform knowledge. |
| Content | `agents/` | Template, built-in skills, built-in tools, safety rules. Pure content. |
| Platform | `server/` | FastAPI + CLI. Orchestration, session/agent lifecycle, API. |
| Terminal UI | `shell/` | Ink shell. Calls server HTTP. Auto-starts backend. |

Rules:

- `engine/` must not know FastAPI, HTTP, or agent instance management
- `server/app/routers/` stays thin — extract params, call service, return result
- `server/app/` is the FastAPI application package; keep this conventional layout
- `agents/smith/` is where Smith's built-in identity seed lives
- New capabilities → add skills or knowledge docs, not new agents

## 6. Files That Matter

| Area | Key Files |
|---|---|
| Terminal entry | `shell/bin/smith.js` → `shell/src/index.tsx` |
| Agent lifecycle | `server/app/services/agent_profile_service.py` |
| Chat + execution | `server/app/services/session_service.py` |
| ReAct loop | `engine/execution/agent_loop.py` |
| Task routing | `engine/execution/task_router.py` |
| Skill chain | `engine/execution/skill_chain.py` |
| Prompt assembly | `engine/prompt/assembler.py` |
| Smith profile seed | `agents/smith/` |
| Terminal shell | `shell/src/index.tsx` |

## 7. Smith Profile System

Only one built-in Smith identity exists. Its source files live in `agents/smith/`; the legacy `personal-assistant` id remains only as a compatibility role/template id for existing API and data paths. Legacy multi-agent templates and bundled skills have been removed; optional skills can still be installed into Smith's runtime profile.

## 8. Implementation Guidance

- Inspect current code first; prefer existing patterns
- Keep changes local; preserve compatibility unless asked to break it
- New domain expertise → knowledge docs injected via assembler, not new templates
- New task workflows → SKILL.md files, not new agents
- Smith identity changes belong in `agents/smith/`; capabilities belong in skills

## 9. Testing And Verification

```bash
cd server && uv run --with pytest --with pytest-asyncio pytest
cd engine && uv run --with pytest --with pytest-asyncio pytest
cd shell && npm run build && npm test
cd server && uv run uvicorn app.main:app --port 8000
```

## 10. Default Decision Rule

If a choice is unclear, prefer the option that:

- makes the single-Agent terminal experience more usable
- reuses existing skill/knowledge infrastructure
- avoids introducing multi-agent complexity
- keeps changes minimal and reversible
