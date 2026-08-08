# AGENTS.md

This file is the working brief for Codex or any coding agent operating in this repository.

**Trust code over docs.** `docs/` has not been kept in sync with refactors. When
this file and the code disagree, the code wins — and fix this file. Count things
by the consumer's criterion, never by directory listing: `engine/tool/registry.py`
accepts a tool only if it defines `TOOL_META` + `execute`; a skill counts only if
its directory holds a top-level `SKILL.md`.

## 1. What This Project Is

Agent-Smith is a local-first personal assistant Agent workbench that runs in the terminal.

- Smith is the single, always-on Agent
- Smith uses the skill system to switch workflows per task type
- No sub-agents, no multi-agent routing — one Agent, one conversation, accumulating memory over time

One-line:

> Agent-Smith is a local-first Agent workbench. Smith is your single resident
> assistant — it keeps context, accumulates memory, and switches workflows via skills.

## 2. Current Priority

The current priority is the terminal workbench experience:

1. Ink shell (`shell/`) is the sole entry point and provides the rich terminal UI
2. Skill-based workflow switching works for different task types
3. Memory accumulation across sessions

## 3. Agent Architecture

```
User ──▶ Ink shell ──HTTP + SSE──▶ server ──▶ engine
                                                │
              identity_catalog (agents/identities/*.yaml)
       keyword score match, priority breaks ties ──▶ RouteDecision
                                                │
                        ┌───────────────────────┴──────────────────┐
                        ▼                                          ▼
                 no pipeline matched                     pipeline route matched
                 direct ReAct loop                   (agents/pipelines/<id>.yaml)
                                                                   │
                                  skill chain nodes, one gate per node
                                  gate fails ──▶ backtrack to an earlier node
```

Declared routes: identity `smith` (default) has `git` (no pipeline); identity
`coding` has `requirements-research`, `tdd-development`, `code-review`, each →
the same-named pipeline. Ordinary coding requests intentionally stay direct
ReAct (see `agents/identities/coding.yaml` instructions). A pipeline whose
skills are not installed falls back to direct ReAct for the whole run, and
ROUTE_DECIDED reports the fallback (no pipeline) rather than the skipped chain.

Prompt assembly (`engine/context/assembler.py`) stacks 12 trust-tagged layers:
Agent Role / Style / Workflow, Tool Usage Policy, Available Tools, Available
Skills, Learned User Context, Global Instructions, Project Instructions,
Identity Guidance, Evaluation Safety Guidance (conditional), Output Style.

## 4. Product Language

Use: "Smith", "Agent", "skill", "session", "memory", "tool", "template"

Avoid: "sub-agent", "employee", "digital employee", "hire"

## 5. Architecture Boundaries

Four layers, one-way dependencies (verified by import graph, not by convention):

```
server/ → engine/ → common/
          ↑
        agents/   (loaded at runtime, never imported)
```

Plus `shell/` as the terminal frontend (Ink/React, calls server over HTTP).

| Layer | Directory | Source lines | Responsibility |
|---|---|---|---|
| Infrastructure | `common/` | 269 | Paths, SQLite connection, YAML read/write. Zero business logic. |
| Execution | `engine/` | 16.5k | Agent framework: LLM, pipeline + ReAct, memory, skills, tools, safety, observability. Zero platform knowledge. |
| Content | `agents/` | 4.0k | Smith identity seed, pipelines, gates, tools, skills, safety rules. Pure content. |
| Platform | `server/` | 5.9k | FastAPI. Orchestration, session/agent lifecycle, 34 HTTP endpoints. |
| Terminal UI | `shell/` | 9.3k TS | Ink shell. Calls server over HTTP, auto-starts the backend. |

Rules:

- `engine/` must not know FastAPI, HTTP, or agent instance management
- `agents/` imports nothing from other layers — the tool registry loads its `.py`
  files via `exec_module`, so the contract is `TOOL_META` + `execute`, not types.
  A path constant cannot be shared into it; expect duplicated path derivation.
- `server/app/routers/` stays thin — extract params, call service, return result
- `server/app/` is the FastAPI application package; keep this conventional layout
- `agents/smith/` is where Smith's built-in identity seed lives
- New capabilities → add skills, not new agents

`common/paths.py` is the single source of truth for the runtime data root
(`~/.agent-smith`, enforced `0o700`/`0o600`). `engine/safety/tool_guard.py`
anchors its non-bypassable platform-write protection on it.

## 6. Files That Matter

| Area | Key Files |
|---|---|
| Terminal entry | `shell/bin/smith.js` → `shell/src/index.tsx` |
| Backend spawn | `shell/src/dev-server.ts` (runs `uv run uvicorn app.main:app`) |
| Engine assembly | `server/app/services/engine_runtime.py` |
| Agent lifecycle | `server/app/services/agent_profile_service.py` |
| Chat + execution | `server/app/services/session_service.py` |
| Run lifecycle | `engine/execution/orchestration/lifecycle.py` |
| Agent dispatch | `engine/execution/orchestration/agent_loop.py` |
| ReAct loop | `engine/execution/react/react_loop.py` |
| Task routing | `engine/execution/routing/task_router.py`, `engine/identity/catalog.py` |
| Pipeline + skill chain | `engine/execution/pipeline/pipeline.py`, `.../skill_chain.py` |
| Prompt assembly | `engine/context/assembler.py` |
| Tool policy | `engine/safety/tool_policy.py` (hard guard before soft challenge) |
| Data root | `common/paths.py` |
| Smith profile seed | `agents/smith/` |

## 7. Smith Profile System

Only one built-in Smith identity exists. Its source files live in `agents/smith/`
(`role.md`, `style.md`, `workflow.md`, `context.md`, `toolbox.md`, `config.yaml`).
The legacy `personal-assistant` id remains only as a compatibility role/template
id (`SMITH_TEMPLATE_ID` in `engine/llm/model_config.py`, `role:` in
`agents/smith/config.yaml`) for existing API and data paths. Legacy multi-agent
templates have been removed; optional skills can still be installed into Smith's
runtime profile.

Skills that ship with Smith are mirrored into `~/.agent-smith/builtin/skills/`,
discovered by scanning `agents/skills/` for directories holding a `SKILL.md`.
A wheel install ships them through `[tool.setuptools.data-files]` in
`common/pyproject.toml`, which needs one entry per skill — a test asserts that
declaration stays in sync.

## 8. Implementation Guidance

- Inspect current code first; prefer existing patterns
- Keep changes local; preserve compatibility unless asked to break it
- New task workflows → `SKILL.md` files, not new agents
- Smith identity changes belong in `agents/smith/`; capabilities belong in skills
- Safety changes: `tool_guard.py` is the non-bypassable boundary, `fact_gate.py`
  only challenges and can be retried. Keep the guard first — a test enforces it.

### Working Rhythm

Skills are matched passively; nothing chains them. Name the step you want:

| Intent | Command |
|---|---|
| Turn a vague ask into a decided plan | `grill me` |
| Execute a defined task | `/ecc:orch-fix-defect`, `/ecc:orch-add-feature`, `/ecc:orch-refine-code` |
| Check a diff before it lands | `/ecc:code-review` |

`grill me` stops at shared understanding — it does not hand off. The `orch-*`
skills delegate to `ecc:orch-pipeline` (Research → Plan → TDD → Review → Commit,
two human gates); invoke an operation skill, never the engine directly.

## 9. Testing And Verification

```bash
cd server && uv run --with pytest --with pytest-asyncio pytest
cd engine && uv run --with pytest --with pytest-asyncio pytest
cd shell && npm run build && npm test
cd server && uv run uvicorn app.main:app --port 8000
```

Current baseline: engine 717 passed, server 155 passed (5 skipped).

## 10. Not Implemented Yet

Product intent recorded here so it is not mistaken for existing behavior:

- **Knowledge injection.** Earlier drafts of this file described injecting
  domain expertise (frontend/backend knowledge docs) on demand. No such
  mechanism exists. `agent_profiles.knowledge` is a `list[str]` column that the
  prompt assembler does not read.

- **Future multi-Agent orchestration.** Smith currently remains a single Agent:
  improve the terminal experience, memory, skills, and the existing pipeline
  path before expanding the execution model. A future direction is a controlled
  coordinator model where Smith plans, delegates bounded domain tasks, enforces
  permissions and quality gates, then synthesizes the final result. This is not
  yet implemented and must not be approximated by ad-hoc sub-Agent calls.
  When implemented, every delegated or side-query Agent must inherit an
  immutable, byte-for-byte cache-aligned prefix from its parent: system and
  developer prompts, tool definitions, model configuration, message prefix, and
  reasoning configuration. Task-specific delegation data belongs only in the
  suffix after that shared prefix, so it does not invalidate prefix-cache reuse.

## 11. Default Decision Rule

If a choice is unclear, prefer the option that:

- makes the single-Agent terminal experience more usable
- reuses existing skill infrastructure
- avoids introducing multi-agent complexity
- keeps changes minimal and reversible
