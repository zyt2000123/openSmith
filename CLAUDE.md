# CLAUDE.md

This file is the working brief for Claude or any coding agent operating in this repository.

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
        identity_catalog (agents/identities/smith.yaml · coding.yaml)
        keyword/example match → LLM fallback (declared routes only) ──▶ RouteDecision
                                                │
                        ┌───────────────────────┴──────────────────┐
                        ▼                                          ▼
                 route: no pipeline                          route: pipeline
                 direct ReAct loop                  (agents/pipelines/*.yaml)
                                                                   │
   requirements-research:  grilling → research → ecc-plan
       gates: grilling_complete / research_brief / plan_confirmed
   tdd-development:  diagnosing-bugs* → tdd-workflow → verification-loop
       gates: red_loop / tdd_evidence / tdd_verification
       * runs only when `coding_bugfix_needs_diagnosis` holds
   code-review:  code-review → verification-loop
       gates: review_report / review_verification
                                                                   │
        gate fails ──▶ node retries with gate feedback (CTX_RETRY_HINT)
```

Declared routes: `agents/identities/coding.yaml` holds the three chain routes —
`requirements-research` (priority 30), `tdd-development` (20), `code-review`
(10); `agents/identities/smith.yaml` holds only `git` (no pipeline). Ordinary
coding, bugfix, and refactor requests are deliberately **not** hijacked by
keywords — they stay in generic ReAct. A pipeline node falls back to generic
ReAct when no matching `SKILL.md` is installed; the gate still runs, so the
intermediate contract stays observable.

Prompt assembly (`engine/context/assembler.py`) stacks 19 trust-tagged layers:
Agent Role / Style / Workflow, Tool Usage Policy, Available Tools, Available
Skills, Learned User Context, Global Instructions, Project Instructions,
Identity Guidance, Evaluation Safety Guidance (conditional), Output Style,
Memory Governance, Durable Memory, Recent Working Context, Durable Memory
Retrieval, Relevant Episodes, Runtime Context, Engine Runtime Control.

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
| Infrastructure | `common/` | 578 | Paths, SQLite connection, YAML read/write. Zero business logic. |
| Execution | `engine/` | 23.1k | Agent framework: LLM, pipeline + ReAct, memory, skills, tools, safety, observability. Zero platform knowledge. |
| Content | `agents/` | 4.8k | Smith identity seed, pipelines, gates, tools, skills, safety rules. Pure content. |
| Platform | `server/` | 5.2k | FastAPI. Orchestration, session/agent lifecycle, 35 HTTP endpoints. |
| Terminal UI | `shell/` | 10.1k TS | Ink shell. Calls server over HTTP, auto-starts the backend. |

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
| **Hook system** | `engine/execution/hooks/` (framework), `agents/smith/hooks/` (built-in implementations) |
| Data root | `common/paths.py` |
| Smith profile seed | `agents/smith/` |

## 6a. Hook System

Smith has a **three-layer Hook system** for tool execution lifecycle management:

### Architecture

```
PreToolUse (can block) → Tool Execution → PostToolUse (warnings only) → Stop (batch processing)
```

**Three Hook types**:

1. **PreToolHook** - Executes before tool call, can block dangerous operations
   - Priority-ordered (lower number = higher priority)
   - Returns `(allowed: bool, denial_reason: str | None)`
   - Use for: security enforcement, policy validation, configuration protection

2. **PostToolHook** - Executes after tool call, observes results
   - Cannot block (tool already executed)
   - Returns `list[str]` of warnings (injected into conversation)
   - Can be async (non-blocking)
   - Use for: quality checks, logging, metrics

3. **StopHook** - Executes at end of each Agent response
   - Typically async (non-blocking)
   - Use for: cost tracking, session persistence, batch processing

### File Layout

```
engine/execution/hooks/
├── hook_interface.py    # Abstract base classes
├── hook_manager.py      # HookRegistry (registration + execution)
├── hook_loader.py       # Dynamic loading from YAML config
├── engine_hooks.py      # HookManager/HookType — engine-internal extension hooks
└── __init__.py

agents/smith/hooks/
├── config_protection.py # PreToolHook: Block config file modifications
├── console_warn.py      # PostToolHook: Warn about debug statements
├── cost_tracker.py      # StopHook: Track token usage and costs
├── fact_gate.py         # PreToolHook: Require investigation before first edit
├── quality_gate.py      # PostToolHook: Run format/lint checks
└── __init__.py

agents/smith/hooks.yaml  # Hook configuration (which hooks are enabled)
```

### Integration Points

- `preparation.py`: Loads hooks from `agents/smith/hooks.yaml` into `services.hook_registry`
- `react_loop.py`: Calls `hook_registry.run_pre_hooks()` before tool execution, `run_post_hooks()` after
- `lifecycle.py`: Can call `hook_registry.run_stop_hooks()` at response end (not yet implemented)

### Built-in Hooks

| Hook | Type | Enabled | Purpose |
|------|------|---------|---------|
| `config-protection` | Pre | ✅ | Block edits to linter/formatter/type-checker configs |
| `console-warn` | Post | ✅ | Warn about `console.log`, `print()`, etc. |
| `quality-gate` | Post | ✅ | Run format/lint checks (async) |
| `cost-tracker` | Stop | ✅ | Write token usage to `~/.agent-smith/metrics/costs.jsonl` |
| `fact-gate` | Pre | ❌ | Require Read before first Edit (needs session state) |

### Extension

Users can add custom hooks:
1. Write a Hook class implementing `PreToolHook`, `PostToolHook`, or `StopHook`
2. Add entry to `~/.agent-smith/hooks.yaml` (loaded after built-in hooks)

Hook system is **pluggable** — engine provides framework, agents provide implementations.

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

Profile seeding is copy-once: `init_smith_profile_files` skips any file that
already exists in `~/.agent-smith/`, so additions to the repo seed (e.g. the
commented `knowledge:` example in `agents/smith/config.yaml`) reach fresh
installs only. Existing installs configure features by editing
`~/.agent-smith/config.yaml` directly.

## 8. Implementation Guidance

- Inspect current code first; prefer existing patterns
- Keep changes local; preserve compatibility unless asked to break it
- New task workflows → `SKILL.md` files, not new agents
- Smith identity changes belong in `agents/smith/`; capabilities belong in skills
- Safety changes: `tool_guard.py` is the non-bypassable boundary, `fact_gate.py`
  only challenges and can be retried. Keep the guard first — a test enforces it.

### Working Rhythm

Intent routing decides between direct ReAct and one of the three shipped
skill chains (§3). Explicit entry points:

| Intent | Entry |
|---|---|
| Turn a vague ask into a decided plan | `grill me` → `grilling` → `research` → `ecc-plan` |
| Requirements research | keywords (`需求调研`, `调研`, `研究一下`, …) → `requirements-research` chain |
| TDD feature/bugfix | keywords (`tdd`, `测试驱动`, `先写测试`, …) → `tdd-development` chain |
| Review a diff before it lands | keywords (`code review`, `代码评审`, `评审一下`, …) → `code-review` chain |
| Everything else | stays in generic direct ReAct |

Routing is lexical first (`IdentityCatalog` keyword/example + priority match),
then an LLM fallback classifier constrained to declared `identity:route`
tokens (`route_task_with_llm`, wired into `prepare_runtime`); neither path can
invent an identity, domain, or pipeline. `grill me` stops at shared
understanding — it does not hand off or implement.

## 9. Testing And Verification

```bash
cd server && uv run --with pytest --with pytest-asyncio pytest
cd engine && uv run --with pytest --with pytest-asyncio pytest
cd shell && npm run build && npm test
cd server && uv run uvicorn app.main:app --port 8000
```

Current baseline: engine 1046 passed (59 skipped), server 237 passed (5 skipped),
shell 301 passed (with `~/.agent-smith/auth_token` present).

The engine's 59 skips are almost all macOS-only Seatbelt tests; every one carries
`@pytest.mark.skipif(sys.platform != "darwin")`. A Seatbelt test *failing* rather
than skipping on Linux means that marker is missing — add it.

`shell` has 12 auth-dependent tests that fail on a container with no
`~/.agent-smith/auth_token`: they call the real `localAuthHeaders`, which reads
that file. Create it (`mkdir -p ~/.agent-smith && printf token > ~/.agent-smith/auth_token
&& chmod 600 ~/.agent-smith/auth_token`) to run the whole suite green, or treat
them as environment noise — they fail identically on `main`, not as a regression
from your change.

## 10. Not Implemented Yet

Product intent recorded here so it is not mistaken for existing behavior:

- **Knowledge injection.** Earlier drafts of this file described injecting
  domain expertise (frontend/backend knowledge docs) on demand. No such
  mechanism exists. `agent_profiles.knowledge` is a `list[str]` column that the
  prompt assembler does not read.

## 11. Default Decision Rule

If a choice is unclear, prefer the option that:

- makes the single-Agent terminal experience more usable
- reuses existing skill infrastructure
- avoids introducing multi-agent complexity
- keeps changes minimal and reversible
